from typing import Callable

import torch
from torch import autograd
import triton
import triton.language as tl

from ..triton_utils import type_dict, contiguous_and_device_guard
from ..triton_utils import amp_custom_fwd, amp_custom_bwd


@triton.autotune(
    configs=[
        triton.Config({"BLOCK_NCL": f * w * 32}, num_warps=w)
        for f in [1, 2]
        for w in [4, 8]
    ],
    key=["T", "NCL", "dtype", "soft_reset", "save_intermediates"],
    restore_value=["s_seq_ptr", "h_seq_ptr", "v_seq_ptr"],
)
@triton.jit
def _multistep_if_forward_kernel(
    x_seq_ptr,  # [T, NCL]
    v_init_ptr,  # [1, NCL]
    s_seq_ptr,
    h_seq_ptr,
    v_seq_ptr,
    v_threshold,
    v_reset,
    T: tl.constexpr,
    NCL: tl.constexpr,
    BLOCK_NCL: tl.constexpr,
    dtype: tl.constexpr,
    soft_reset: tl.constexpr,
    save_intermediates: tl.constexpr,
):
    pid_ncl = tl.program_id(0)
    ncl_offset = pid_ncl * BLOCK_NCL

    v_init_ptrs = tl.make_block_ptr(
        v_init_ptr,
        shape=(1, NCL),
        strides=(NCL, 1),
        offsets=(0, ncl_offset),
        block_shape=(1, BLOCK_NCL),
        order=(1, 0)
    )
    v = tl.load(v_init_ptrs, boundary_check=(1,), padding_option="zero")

    for t in tl.static_range(0, T, 1):
        x_ptrs = tl.make_block_ptr(
            x_seq_ptr,
            shape=(T, NCL),
            strides=(NCL, 1),
            offsets=(t, ncl_offset),
            block_shape=(1, BLOCK_NCL),
            order=(1, 0)
        )
        x = tl.load(x_ptrs, boundary_check=(1,), padding_option="zero")

        h = v + x
        s = (h >= v_threshold).to(dtype)
        if soft_reset:
            v = h - s*v_threshold
        else:
            v = s*v_reset + (1.-s) * h

        s_ptrs = tl.make_block_ptr(
            s_seq_ptr,
            shape=(T, NCL),
            strides=(NCL, 1),
            offsets=(t, ncl_offset),
            block_shape=(1, BLOCK_NCL),
            order=(1, 0)
        )
        tl.store(s_ptrs, s, boundary_check=(1,))
        v_ptrs = tl.make_block_ptr(
            v_seq_ptr,
            shape=(T, NCL),
            strides=(NCL, 1),
            offsets=(t, ncl_offset),
            block_shape=(1, BLOCK_NCL),
            order=(1, 0)
        )
        tl.store(v_ptrs, v, boundary_check=(1,))
        if save_intermediates:
            h_ptrs = tl.make_block_ptr(
                h_seq_ptr,
                shape=(T, NCL),
                strides=(NCL, 1),
                offsets=(t, ncl_offset),
                block_shape=(1, BLOCK_NCL),
                order=(1, 0)
            )
            tl.store(h_ptrs, h, boundary_check=(1,))


@triton.autotune(
    configs=[
        triton.Config({"BLOCK_NCL": f * w * 32}, num_warps=w)
        for f in [1, 2]
        for w in [4, 8]
    ],
    key=["T", "NCL", "dtype", "soft_reset", "detach_reset"],
    restore_value=["grad_x_seq_ptr"],
)
@triton.jit
def _multistep_if_backward_kernel(
    grad_s_seq_ptr,
    grad_v_seq_ptr,
    h_seq_ptr,
    grad_x_seq_ptr,
    grad_v_init_ptr,
    v_threshold,
    v_reset,
    T: tl.constexpr,
    NCL: tl.constexpr,
    BLOCK_NCL: tl.constexpr,
    dtype: tl.constexpr,  # grad_s_seq.dtype; might != h_seq or s_seq.dtype
    sg_fn: tl.constexpr,
    soft_reset: tl.constexpr,
    detach_reset: tl.constexpr,
):
    pid_ncl = tl.program_id(0)
    ncl_offset = pid_ncl * BLOCK_NCL

    grad_v_acc = tl.zeros([1, BLOCK_NCL], dtype=dtype)

    for t in tl.static_range(T - 1, -1, -1):
        grad_s_ptrs = tl.make_block_ptr(
            grad_s_seq_ptr,
            shape=(T, NCL),
            strides=(NCL, 1),
            offsets=(t, ncl_offset),
            block_shape=(1, BLOCK_NCL),
            order=(1, 0)
        )
        grad_s = tl.load(
            grad_s_ptrs, boundary_check=(1,), padding_option="zero"
        )
        grad_v_ptrs = tl.make_block_ptr(
            grad_v_seq_ptr,
            shape=(T, NCL),
            strides=(NCL, 1),
            offsets=(t, ncl_offset),
            block_shape=(1, BLOCK_NCL),
            order=(1, 0)
        )
        grad_v = tl.load(
            grad_v_ptrs, boundary_check=(1,), padding_option="zero"
        )
        h_ptrs = tl.make_block_ptr(
            h_seq_ptr,
            shape=(T, NCL),
            strides=(NCL, 1),
            offsets=(t, ncl_offset),
            block_shape=(1, BLOCK_NCL),
            order=(1, 0)
        )
        h = tl.load(h_ptrs, boundary_check=(1,), padding_option="zero")

        sg = sg_fn(h - v_threshold)
        grad_v_combined = grad_v + grad_v_acc
        if soft_reset:
            if detach_reset:
                grad_h = tl.fma(grad_s, sg, grad_v_combined)
            else:
                grad_h = tl.fma(
                    grad_s - v_threshold*grad_v_combined, sg, grad_v_combined
                )
        else:
            s = (h >= v_threshold).to(dtype)
            if detach_reset:
                grad_h = tl.fma(grad_s, sg, grad_v_combined * (1.-s))
            else:
                grad_h = tl.fma(
                    tl.fma(grad_v_combined, v_reset - h, grad_s),
                    sg,
                    grad_v_combined * (1.-s),
                )
        grad_v_acc = grad_h
        grad_x = grad_h

        grad_x_ptrs = tl.make_block_ptr(
            grad_x_seq_ptr,
            shape=(T, NCL),
            strides=(NCL, 1),
            offsets=(t, ncl_offset),
            block_shape=(1, BLOCK_NCL),
            order=(1, 0)
        )
        tl.store(grad_x_ptrs, grad_x.to(dtype), boundary_check=(1,))

    grad_v_init_ptrs = tl.make_block_ptr(
        grad_v_init_ptr,
        shape=(1, NCL),
        strides=(NCL, 1),
        offsets=(0, ncl_offset),
        block_shape=(1, BLOCK_NCL),
        order=(1, 0)
    )
    tl.store(grad_v_init_ptrs, grad_v_acc.to(dtype), boundary_check=(1,))


def multistep_if_inference(
    x_seq: torch.Tensor,
    v_init: torch.Tensor,
    v_threshold: float,
    v_reset: float,
    soft_reset: bool,
):
    T = x_seq.shape[0]
    NCL = x_seq[0].numel()
    s_seq, v_seq = torch.empty_like(x_seq), torch.empty_like(x_seq)
    dtype = x_seq.dtype
    grid = lambda meta: (triton.cdiv(NCL, meta['BLOCK_NCL']),)

    _multistep_if_forward_kernel[grid](
        x_seq,
        v_init,
        s_seq,
        None,
        v_seq,
        v_threshold,
        v_reset,
        T=T,
        NCL=NCL,
        dtype=type_dict[dtype],
        soft_reset=soft_reset,
        save_intermediates=False,
    )
    return s_seq, v_seq


def multistep_if_forward(
    x_seq: torch.Tensor,
    v_init: torch.Tensor,
    v_threshold: float,
    v_reset: float,
    soft_reset: bool,
):
    T = x_seq.shape[0]
    NCL = x_seq[0].numel()
    s_seq, v_seq = torch.empty_like(x_seq), torch.empty_like(x_seq)
    h_seq = torch.empty_like(x_seq)
    dtype = x_seq.dtype
    grid = lambda meta: (triton.cdiv(NCL, meta['BLOCK_NCL']),)

    _multistep_if_forward_kernel[grid](
        x_seq,
        v_init,
        s_seq,
        h_seq,
        v_seq,
        v_threshold,
        v_reset,
        T=T,
        NCL=NCL,
        dtype=type_dict[dtype],
        soft_reset=soft_reset,
        save_intermediates=True,
    )
    return s_seq, v_seq, h_seq


def multistep_if_backward(
    grad_s_seq: torch.Tensor,
    grad_v_seq: torch.Tensor,
    h_seq: torch.Tensor,
    v_threshold: float,
    v_reset: float,
    sg_fn: Callable,
    soft_reset: bool,
    detach_reset: bool,
):
    T = grad_s_seq.shape[0]
    NCL = grad_s_seq[0].numel()
    grad_x_seq = torch.empty_like(grad_s_seq)
    grad_v_init = torch.empty_like(grad_v_seq[0])
    dtype = grad_s_seq.dtype
    grid = lambda meta: (triton.cdiv(NCL, meta['BLOCK_NCL']),)

    _multistep_if_backward_kernel[grid](
        grad_s_seq,
        grad_v_seq,
        h_seq,
        grad_x_seq,
        grad_v_init,
        v_threshold,
        v_reset,
        T=T,
        NCL=NCL,
        dtype=type_dict[dtype],
        sg_fn=sg_fn,
        soft_reset=soft_reset,
        detach_reset=detach_reset,
    )
    return grad_x_seq, grad_v_init


class MultiStepIFNodePTT(autograd.Function):

    @staticmethod
    @contiguous_and_device_guard
    @amp_custom_fwd
    def forward(
        ctx, x_seq: torch.Tensor, v_init: torch.Tensor, v_threshold: float, 
        v_reset: float, detach_reset: bool, sg_fn: Callable
    ):
        soft_reset = v_reset is None
        v_reset = v_reset if v_reset is not None else 0.
        if any(ctx.needs_input_grad):
            s_seq, v_seq, h_seq = multistep_if_forward(
                x_seq, v_init, v_threshold, v_reset, soft_reset
            )
            ctx.save_for_backward(h_seq)
            ctx.v_threshold = v_threshold
            ctx.v_reset = v_reset
            ctx.soft_reset = soft_reset
            ctx.detach_reset = detach_reset
            ctx.sg_fn = sg_fn
        else:
            s_seq, v_seq = multistep_if_inference(
                x_seq, v_init, v_threshold, v_reset, soft_reset
            )
        return s_seq, v_seq

    @staticmethod
    @contiguous_and_device_guard
    @amp_custom_bwd
    def backward(ctx, grad_s_seq: torch.Tensor, grad_v_seq: torch.Tensor):
        h_seq = ctx.saved_tensors[0]
        grad_x_seq, grad_v_init = multistep_if_backward(
            grad_s_seq, grad_v_seq, h_seq, ctx.v_threshold,
            ctx.v_reset, ctx.sg_fn, ctx.soft_reset, ctx.detach_reset
        )
        return grad_x_seq, grad_v_init, None, None, None, None
