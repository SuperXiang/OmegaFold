# -*- coding: utf-8 -*-
# =============================================================================
# Copyright 2022 HeliXon Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================
"""

"""
# =============================================================================
# Imports
# =============================================================================
import argparse
import numbers
import typing

import torch
from torch import nn

from omegafold import utils


# =============================================================================
# Constants
# =============================================================================
# =============================================================================
# Functions
# =============================================================================
def softmax(
        x: torch.Tensor,
        dim: int,
        *,
        dtype: typing.Optional[torch.dtype] = None,
        in_place: bool = False
) -> torch.Tensor:
    """
    In-place or normal softmax

    Args:
        x: the input tensor
        dim: the dimension along which to perform the softmax
        dtype: the data type
        in_place: if to perform inplace

    Returns:

    """
    if in_place:
        max_val = torch.max(x, dim=dim, keepdim=True)[0]
        torch.sub(x, max_val, out=x)
        torch.exp(x, out=x)
        summed = torch.sum(x, dim=dim, keepdim=True)
        x /= summed
        return x
    else:
        return torch.softmax(input=x, dim=dim, dtype=dtype)


def _attention(
        query: torch.Tensor,
        key: torch.Tensor,
        scale: torch.Tensor,
        value: torch.Tensor,
        bias: torch.Tensor,
) -> typing.Tuple[torch.Tensor, torch.Tensor]:
    """Normal attention

    Args:
        query: positive tensor of shape (*_q, dim_qk)
        key: positive tensor of shape (*_k, dim_qk)
        scale: the scaling of logits
        value: tensor of shape (*_k, dim_v)
        bias: the bias acting as either mask or relative positional encoding

    Returns:
        The aggregated tensor of shape (*_q, dim_v)

    """
    logits = torch.einsum("...id, ...jd -> ...ij", query * scale, key)
    attn = softmax(logits + bias, dim=-1, in_place=True)
    out = torch.einsum("...ij, ...jd -> ...id", attn, value)
    return out, attn


def attention(
        query: torch.Tensor,
        key: torch.Tensor,
        scale: typing.Union[torch.Tensor, float],
        value: torch.Tensor,
        bias: torch.Tensor,
        subbatch_size: typing.Optional[int] = None
) -> typing.Tuple[torch.Tensor, torch.Tensor]:
    """Computes attention with q, k , v

    Args:
        query: positive tensor of shape (*_q, dim_qk)
        key: positive tensor of shape (*_k, dim_qk)
        scale: the scaling of logits
        value: tensor of shape (*_k, dim_v)
        bias: the bias acting as either mask or relative positional encoding
        subbatch_size: the subbatch size to split the computation into

    Returns:
        The aggregated tensor of shape (*_q, dim_v)

    """
    q_length, k_length, v_dim = query.shape[-2], key.shape[-2], value.shape[-1]
    subbatch_size = subbatch_size or q_length

    batch_shape = query.shape[:-2]
    factory_kwargs = nn.factory_kwargs(
        {"device": query.device, "dtype": query.dtype}
    )
    output = torch.empty(*batch_shape, q_length, v_dim, **factory_kwargs)
    logits = torch.empty(*batch_shape, q_length, k_length, **factory_kwargs)

    for i, q_i in enumerate(query.split(subbatch_size, dim=-2)):
        start, end = i * subbatch_size, (i + 1) * subbatch_size,
        if bias.shape[-2] != q_length:
            b_i = bias
        else:
            b_i = bias[..., start:end, :]

        res, attn = _attention(q_i, key, scale, value, b_i)
        output[..., start:end, :] = res
        logits[..., start:end, :] = attn

    return output, logits


# =============================================================================
# Classes
# =============================================================================

class OFModule(nn.Module):
    """
    The OmegaFold modules
        args: The arguments used for each of the modules
    """

    def __init__(
            self,
            cfg: typing.Optional[argparse.Namespace]
    ) -> None:
        super(OFModule, self).__init__()
        self.cfg = cfg

    @property
    def device(self) -> torch.device:
        return next(self.parameters()).device

    @property
    def dtype(self) -> torch.dtype:
        return next(self.parameters()).dtype


class Transition(OFModule):
    def __init__(self, d: int, n: int, activation: str) -> None:
        super(Transition, self).__init__(None)
        fc1 = nn.Linear(d, n * d)
        fc2 = nn.Linear(n * d, d)
        try:
            act = getattr(nn, activation)(inplace=True)
        except TypeError:
            act = getattr(nn, activation)()
        self.network = nn.Sequential(fc1, act, fc2)

    def forward(
            self,
            x: torch.Tensor,
            subbatch_size: typing.Optional[int]
    ) -> torch.Tensor:
        subbatch_size = subbatch_size or x.shape[-2]

        out = torch.empty_like(x)
        for i, x_i in enumerate(x.split(subbatch_size, dim=0)):
            start, end = i * subbatch_size, (i + 1) * subbatch_size
            x_i = utils.normalize(x_i)
            out[start:end] = self.network(x_i)
        return out


class MultiHeadedScaling(OFModule):
    """
    Perform an element wise scale shift

    """

    def __init__(
            self,
            shape: typing.Union[int, typing.List[int], torch.Size],
            num_heads: int,
            on_out_ready: typing.Optional[
                typing.Callable[[torch.Tensor], torch.Tensor]
            ],
            dtype: typing.Optional[torch.dtype] = None,
    ) -> None:
        """

        Args:
            shape: the shape of the input dimensions
            num_heads: the number of dimensions to squeeze to
            dtype: the dtype of the parameters at generation
            on_out_ready: the function called on exit
        """
        super(MultiHeadedScaling, self).__init__(None)
        factory_kwargs = nn.factory_kwargs({"dtype": dtype})
        if isinstance(shape, numbers.Integral):
            shape = (shape,)
        shape = list(tuple(shape))
        self.unsqueeze_dim = - (len(shape) + 1)
        shape.insert(0, num_heads)
        self.shape = shape
        self.split_dims = [1] * num_heads
        self.weight = nn.Parameter(torch.empty(self.shape, **factory_kwargs))
        self.bias = nn.Parameter(torch.empty(self.shape, **factory_kwargs))
        self.call_on_out_ready = on_out_ready

        self.reset_parameters()

    def forward(self, x: torch.Tensor) -> typing.List[torch.Tensor]:
        """
        Element wise multiplication followed by addition

        Args:
            x: the input tensor with the trailing dimensions following
                ~self.shape

        Returns:
            A output tensor of the same shape

        """
        x = x.unsqueeze(self.unsqueeze_dim) * self.weight + self.bias
        positive_index = x.ndim + self.unsqueeze_dim
        if self.call_on_out_ready is not None:
            x = self.call_on_out_ready(x)

        x = x.split(self.split_dims, dim=positive_index)

        return [x_i.squeeze(positive_index) for x_i in x]

    def reset_parameters(self):
        nn.init.normal_(self.weight, std=0.02)
        nn.init.zeros_(self.bias)


class Val2Bins(OFModule):
    """
    Convert continuous values to bins

    Attributes:
        breaks: the line space break
    """

    def __init__(self, cfg: argparse.Namespace) -> None:
        super(Val2Bins, self).__init__(cfg)
        self.register_buffer(
            "breaks", torch.linspace(
                cfg.first_break, cfg.last_break, cfg.num_bins - 1
            ), persistent=False
        )

    def forward(self, dist: torch.Tensor) -> torch.Tensor:
        """

        Args:
            dist: distances in the euclidean space.

        Returns:

        """
        dist = dist.unsqueeze(-1)
        dist_bin = torch.sum(
            torch.gt(dist, self.breaks), dim=-1, dtype=torch.long
        )
        return dist_bin


class Node2Edge(OFModule):
    """Communicate between tracks

        faster than OutProductMean mostly due to a better implementation
    """

    def __init__(self, in_dim: int, proj_dim: int, out_dim: int) -> None:
        super(Node2Edge, self).__init__(None)
        self.input_proj = nn.Linear(in_dim, proj_dim * 2)
        self.proj_dim = proj_dim
        self.out_weights = nn.Parameter(
            torch.empty(proj_dim, proj_dim, out_dim)
        )
        self.out_bias = nn.Parameter(torch.empty(out_dim))

    def forward(
            self, node_repr: torch.Tensor, mask: torch.Tensor
    ) -> torch.Tensor:
        node_repr = utils.normalize(node_repr)
        act = self.input_proj(node_repr)
        mask = mask[..., None]
        act = act * mask
        norm = torch.einsum("...sid, ...sjd->...ijd", mask, mask)

        l, r = act.split(self.proj_dim, dim=-1)
        # We found this implementation to work significantly faster
        out = torch.einsum(
            '...sid, def, ...sje-> ...ijf', l, self.out_weights, r
        ) + self.out_bias
        out = out / (norm + 1e-3)

        return out


class Attention(OFModule):
    """
    Widely used attention mechanism

    Attributes:
        qg_weights (nn.Parameter): weight matrices for queries and gates
        qg_bias (nn.Parameter): biases for queries and gates
        kv_weights (nn.Parameter): weight matrices for queries and gates
        kv_bias (nn.Linear): biases for keys and values

        o_weights (nn.Linear): the output weight matrix
        o_bias (nn.Linear): the output bias
    """

    def __init__(
            self,
            q_dim: int,
            kv_dim: int,
            n_head: int,
            gating: bool,
            c: int,
            out_dim: int,
            n_axis: int
    ) -> None:
        super(Attention, self).__init__(None)
        self.c = c
        self.n_head = n_head
        self.gating = gating
        self.q_dim = q_dim
        self.n_axis = n_axis

        self.qg_weights = nn.Parameter(
            torch.empty(q_dim, n_axis, n_head, (gating + 1) * c)
        )
        self.kv_weights = nn.Parameter(
            torch.empty(kv_dim, n_axis, n_head, 2 * c)
        )
        self.qg_bias = nn.Parameter(
            torch.empty(n_axis, n_head, 1, c * (1 + gating))
        )
        self.kv_bias = nn.Parameter(torch.empty(n_axis, n_head, 1, c * 2))

        self.o_weights = nn.Parameter(torch.empty(n_axis, n_head, c, out_dim))
        self.o_bias = nn.Parameter(torch.empty([out_dim, n_axis]))

    def forward(
            self,
            q_inputs: torch.Tensor,
            kv_inputs: torch.Tensor,
            bias: torch.Tensor,
            *,
            fwd_cfg: typing.Optional[argparse.Namespace] = None
    ) -> typing.Union[typing.Tuple[torch.Tensor, torch.Tensor], torch.Tensor]:
        """
        Perform the standard multi-headed attention with added gating with some
        biases

        Args:
            q_inputs: the inputs to generate query vectors,
                of shape (*, q_len, q_dim, (n_axis))
            kv_inputs: the inputs to generate key and value vectors,
                of shape (*, kv_len, kv_dim, (n_axis))
            bias: the bias for the logits
                of shape (*, n_head, q_len, kv_len)
            fwd_cfg: if return logits

        Return:
            output tensor (*, seq_len, o_dim, (n_axis))
            attention logits (Optional) (q_len, kv_len, num_head)
        """

        # Acquire the q, k, v tensors
        to_unsqueeze = (
                q_inputs.shape[-1] != self.n_axis and
                q_inputs.shape[-1] == self.q_dim
        )
        if to_unsqueeze:
            q_inputs = q_inputs.unsqueeze(-1)
            kv_inputs = kv_inputs.unsqueeze(-1)
            if bias is not None:
                bias = bias.unsqueeze(-4)

        qg = torch.einsum('...qar,arhc->...rhqc', q_inputs, self.qg_weights)
        qg += self.qg_bias
        q_out = qg.split(self.c, dim=-1)
        q = q_out[0]

        kv = torch.einsum('...kar,arhc->...rhkc', kv_inputs, self.kv_weights)
        kv += self.kv_bias
        k, v = kv.split([self.c, self.c], dim=-1)

        # Attention
        subbatch_size = (
            q.shape[-4] if fwd_cfg is None else fwd_cfg.subbatch_size
        )
        attn_out, logits = attention(
            query=q,
            key=k,
            value=v,
            subbatch_size=subbatch_size,
            bias=bias,
            scale=self.c ** (-0.5)
        )

        # get the gating
        if self.gating:
            g = torch.sigmoid(q_out[1])
            attn_out *= g

        output = torch.einsum('...rhqc,rhco->...qor', attn_out, self.o_weights)
        output += self.o_bias

        if to_unsqueeze:
            output, logits = output.squeeze(-1), logits.squeeze(-4)
        return output, logits[..., None].transpose(-1, -4).squeeze(-4)


class AttentionWEdgeBias(OFModule):
    def __init__(
            self,
            d_node: int,
            d_edge: int,
            n_head: int,
            attn_gating: bool,
            attn_c: int
    ) -> None:
        super(AttentionWEdgeBias, self).__init__(None)
        self.proj_edge_bias = nn.Linear(
            in_features=d_edge, out_features=n_head  # , bias=False
        )
        self.attention = Attention(
            q_dim=d_node,
            kv_dim=d_node,
            n_head=n_head,
            gating=attn_gating,
            c=attn_c,
            out_dim=d_node,
            n_axis=1
        )

    def forward(
            self,
            node_repr: torch.Tensor,
            edge_repr: torch.Tensor,
            mask: torch.Tensor,
            *,
            fwd_cfg: typing.Optional[argparse.Namespace] = None
    ) -> typing.Union[torch.Tensor, typing.Tuple[torch.Tensor, torch.Tensor]]:
        """

        Args:
            node_repr:
            edge_repr:
            mask:
            fwd_cfg:

        Returns:

        """
        node_repr = utils.normalize(node_repr)
        edge_repr = utils.normalize(edge_repr)
        # check dim
        edge_bias = self.proj_edge_bias(edge_repr).permute(2, 0, 1)

        bias = edge_bias + utils.mask2bias(mask[..., None, None, :])
        attn_out, _ = self.attention(
            node_repr, node_repr, bias=bias, fwd_cfg=fwd_cfg
        )
        return attn_out


class GeometricAttention(OFModule):
    def __init__(self, d_edge: int, c: int, n_head: int, n_axis: int) -> None:
        super(GeometricAttention, self).__init__(None)
        self.d_edge = d_edge
        self.linear_b_weights = nn.Parameter(
            torch.empty([d_edge, n_axis, n_head])
        )
        self.linear_b_bias = nn.Parameter(
            torch.empty([n_axis, n_head, 1, 1])
        )

        self.act_w = nn.Parameter(
            torch.empty([d_edge, n_axis, d_edge * 5])
        )
        self.act_b = nn.Parameter(torch.empty([n_axis, d_edge * 5]))

        self.out_proj_w = nn.Parameter(
            torch.empty([n_axis, d_edge, d_edge])
        )
        self.out_proj_b = nn.Parameter(torch.empty([n_axis, d_edge]))
        self.glu = nn.GLU()

        self.attention = Attention(
            q_dim=d_edge,
            kv_dim=d_edge,
            n_head=n_head,
            c=c,
            gating=True,
            out_dim=d_edge,
            n_axis=n_axis
        )

    def forward(
            self, edge_repr: torch.Tensor, mask: torch.Tensor
    ) -> torch.Tensor:
        edge_repr = utils.normalize(edge_repr)
        edge_stacked = torch.stack(
            [edge_repr, edge_repr.transpose(-2, -3)], dim=-1
        )  # B, L, L, D, R

        b = torch.einsum(
            '...qkcr,crh->...rhqk', edge_stacked, self.linear_b_weights
        ) + self.linear_b_bias
        b += utils.mask2bias(mask[..., None, None, None, :])
        attended, _ = self.attention(edge_stacked, edge_stacked, b)

        act = torch.einsum(
            '...dr,drc->...rc', edge_stacked, self.act_w
        ) + self.act_b
        act, g = act.split([self.d_edge * 4, self.d_edge], dim=-1)

        act = self.glu(act)
        edge_mask = mask[None, ...] * mask[..., None]
        act *= edge_mask[..., None, None]

        ab = torch.einsum(
            '...ikrd,...jkrd->...ijrd',
            *act.split([self.d_edge, self.d_edge], dim=-1)
        ).contiguous()
        ab = utils.normalize(ab)
        ab = torch.einsum(
            '...rd,rdc->...rc', ab, self.out_proj_w
        ) + self.out_proj_b
        gated = ab * g.sigmoid()

        out = gated[..., 0, :] + gated[..., 1, :] + (
                attended[..., 0] + attended[..., 1].transpose(-2, -3))

        return out


class Val2ContBins(OFModule):
    def __init__(self, cfg: argparse.Namespace, ):
        super(Val2ContBins, self).__init__(cfg)

        x_bin_size = (cfg.x_max - cfg.x_min) / (cfg.x_bins - 2)

        self.register_buffer(
            "x_offset", torch.linspace(
                cfg.x_min - x_bin_size / 2,
                cfg.x_max + x_bin_size / 2,
                cfg.x_bins
            ), persistent=False
        )
        self.coeff = -0.5 / ((x_bin_size * 0.2) ** 2)

    def forward(self, dist_x):  # (*)
        x_offset_shape = [1] * len(dist_x.size()) + [len(self.x_offset)]
        x = dist_x.unsqueeze(-1) - self.x_offset.view(*x_offset_shape)
        x_norm = self.coeff * torch.pow(x, 2)
        x_norm = x_norm - x_norm.max(-1, keepdim=True)[0]
        logits = torch.softmax(x_norm, dim=-1)

        return logits


# =============================================================================
# Tests
# =============================================================================
if __name__ == '__main__':
    pass
