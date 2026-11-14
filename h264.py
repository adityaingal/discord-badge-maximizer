#!/usr/bin/env python3
"""
H.264 SPS utilities for the Discord Go Live streamer.

Discord's media workers reject H.264 streams whose Sequence Parameter Set does
not carry the VUI bitstream restriction block. The stream is accepted (the
voice WebSocket handshake completes and the account shows as LIVE) but the
frames are dropped, so viewers never see any video.

Discord requires in the SPS VUI:

    bitstream_restriction_flag = 1
    max_num_reorder_frames     = 0

libx264 never emits those flags - not for baseline, main or high profile - so
the SPS has to be rewritten in software before it is packetized.

The module implements a focused H.264 SPS parser/rewriter:

  * emulation prevention bytes are stripped before parsing and re-applied after
  * the RBSP is parsed bit by bit up to the VUI
  * the VUI is re-emitted with the bitstream restriction block forced on and
    max_num_reorder_frames / max_dec_frame_buffering forced to zero
  * if the SPS carries no VUI at all, a minimal one is appended
  * a NAL that cannot be parsed is returned unchanged (a corrupt SPS is worse
    than one that merely lacks the restriction flags)
"""
from __future__ import annotations

NAL_SPS = 7
NAL_PPS = 8

_HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}


class BitError(Exception):
    """Raised when the bitstream cannot be parsed."""


class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.consumed: list[int] = []

    @property
    def bits_left(self) -> int:
        return len(self.data) * 8 - self.pos

    def u(self, n: int) -> int:
        if n == 0:
            return 0
        if self.pos + n > len(self.data) * 8:
            raise BitError("read past end of rbsp")
        v = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            bit = (byte >> (7 - (self.pos & 7))) & 1
            v = (v << 1) | bit
            self.consumed.append(bit)
            self.pos += 1
        return v

    def ue(self) -> int:
        zeros = 0
        while True:
            if self.pos >= len(self.data) * 8:
                raise BitError("read past end of rbsp")
            if self.u(1):
                break
            zeros += 1
            if zeros > 32:
                raise BitError("invalid exp-golomb code")
        if zeros == 0:
            return 0
        return (1 << zeros) - 1 + self.u(zeros)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)

    def take(self, n: int) -> list[int]:
        return [self.u(1) for _ in range(n)]


class BitWriter:
    def __init__(self, bits: list[int] | None = None) -> None:
        self.bits: list[int] = list(bits) if bits else []

    def u(self, value: int, n: int) -> None:
        for i in range(n - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def ue(self, value: int) -> None:
        value += 1
        length = value.bit_length()
        self.u(0, length - 1)
        self.u(value, length)

    def trailing(self) -> None:
        """rbsp_trailing_bits: a stop bit followed by zero padding."""
        self.bits.append(1)
        while len(self.bits) % 8:
            self.bits.append(0)

    def to_bytes(self) -> bytes:
        out = bytearray()
        for i in range(0, len(self.bits), 8):
            chunk = self.bits[i : i + 8]
            while len(chunk) < 8:
                chunk.append(0)
            byte = 0
            for bit in chunk:
                byte = (byte << 1) | bit
            out.append(byte)
        return bytes(out)


def unescape_rbsp(data: bytes) -> bytes:
    """Remove H.264 emulation prevention bytes (0x03)."""
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def escape_rbsp(data: bytes) -> bytes:
    """Re-apply H.264 emulation prevention bytes (0x03)."""
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte <= 0x03:
            out.append(0x03)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _skip_scaling_list(reader: BitReader, count: int) -> None:
    last_scale = 8
    next_scale = 8
    for index in range(count):
        if next_scale != 0:
            delta = reader.se()
            next_scale = (last_scale + delta + 256) % 256
        last_scale = next_scale if next_scale != 0 else last_scale


def _skip_hrd(reader: BitReader) -> None:
    cpb_cnt_minus1 = reader.ue()
    reader.u(4)  # bit_rate_scale
    reader.u(4)  # cpb_size_scale
    for _ in range(cpb_cnt_minus1 + 1):
        reader.ue()  # bit_rate_value_minus1
        reader.ue()  # cpb_size_value_minus1
        reader.u(1)  # cbr_flag
    reader.u(4)  # initial_cpb_removal_delay_length_minus1
    reader.u(4)  # cpb_removal_delay_length_minus1
    reader.u(4)  # dpb_output_delay_length_minus1
    reader.u(4)  # time_offset_length


def _parse_sps_prefix(reader: BitReader) -> None:
    """Consume every SPS field up to (and including) vui_parameters_present_flag."""
    profile_idc = reader.u(8)
    reader.u(8)  # constraint_set flags + reserved_zero_2bits
    reader.u(8)  # level_idc
    reader.ue()  # seq_parameter_set_id

    if profile_idc in _HIGH_PROFILES:
        chroma_format_idc = reader.ue()
        if chroma_format_idc == 3:
            reader.u(1)  # separate_colour_plane_flag
        reader.ue()  # bit_depth_luma_minus8
        reader.ue()  # bit_depth_chroma_minus8
        reader.u(1)  # qpprime_y_zero_transform_bypass_flag
        if reader.u(1):  # seq_scaling_matrix_present_flag
            count = 8 if chroma_format_idc == 3 else 12
            for index in range(count):
                if reader.u(1):
                    _skip_scaling_list(reader, 16 if index < 6 else 64)

    reader.ue()  # log2_max_frame_num_minus4
    pic_order_cnt_type = reader.ue()
    if pic_order_cnt_type == 0:
        reader.ue()  # log2_max_pic_order_cnt_lsb_minus4
    elif pic_order_cnt_type == 1:
        reader.u(1)  # delta_pic_order_always_zero_flag
        reader.se()  # offset_for_non_ref_pic
        reader.se()  # offset_for_top_to_bottom_field
        for _ in range(reader.ue()):  # offset_for_ref_frame
            reader.se()
    else:
        reader.ue()  # log2_max_pic_order_cnt_lsb_minus4

    reader.ue()  # max_num_ref_frames
    reader.u(1)  # gaps_in_frame_num_value_allowed_flag
    reader.ue()  # pic_width_in_mbs_minus1
    reader.ue()  # pic_height_in_map_units_minus1
    if not reader.u(1):  # frame_mbs_only_flag
        reader.u(1)  # mb_adaptive_frame_field_flag
    reader.u(1)  # direct_8x8_inference_flag
    if reader.u(1):  # frame_cropping_flag
        for _ in range(4):
            reader.ue()
    reader.u(1)  # vui_parameters_present_flag


def _parse_vui(reader: BitReader) -> list[tuple]:
    """Parse the VUI, returning the field list so it can be re-emitted."""
    fields: list[tuple] = []

    def flag(name: str) -> None:
        fields.append(("flag", name, reader.u(1), 1))

    def ue(name: str) -> None:
        fields.append(("ue", name, reader.ue(), 0))

    def u(name: str, n: int) -> None:
        fields.append(("u", name, reader.u(n), n))

    flag("aspect_ratio_info_present_flag")
    if fields[-1][2]:
        u("aspect_ratio_idc", 8)
        if fields[-1][2] == 255:
            u("sar_width", 16)
            u("sar_height", 16)

    flag("overscan_info_present_flag")
    if fields[-1][2]:
        flag("overscan_appropriate_flag")

    flag("video_signal_type_present_flag")
    if fields[-1][2]:
        u("video_format", 3)
        flag("video_full_range_flag")
        flag("colour_description_present_flag")
        if fields[-1][2]:
            u("colour_primaries", 8)
            u("transfer_characteristics", 8)
            u("matrix_coeffs", 8)

    flag("chroma_loc_info_present_flag")
    if fields[-1][2]:
        ue("chroma_sample_loc_type_top_field")
        ue("chroma_sample_loc_type_bottom_field")

    flag("timing_info_present_flag")
    if fields[-1][2]:
        u("num_units_in_tick", 32)
        u("time_scale", 32)
        flag("fixed_frame_rate_flag")

    flag("nal_hrd_parameters_present_flag")
    if fields[-1][2]:
        _skip_hrd(reader)
    flag("vcl_hrd_parameters_present_flag")
    if fields[-1][2]:
        _skip_hrd(reader)
    if fields[-2][2] or fields[-3][2]:
        flag("low_delay_hrd_flag")

    flag("pic_struct_present_flag")
    flag("bitstream_restriction_flag")
    if fields[-1][2]:
        flag("motion_vectors_over_pic_boundaries_flag")
        ue("max_bytes_per_pic_denom")
        ue("max_bits_per_mb_denom")
        ue("log2_max_mv_length_horizontal")
        ue("log2_max_mv_length_vertical")
        ue("max_num_reorder_frames")
        ue("max_dec_frame_buffering")

    return fields


def _emit_vui(writer: BitWriter, fields: list[tuple]) -> None:
    """Re-emit the VUI with the bitstream restriction block Discord requires."""
    had_restriction = False
    for kind, name, value, width in fields:
        if name == "bitstream_restriction_flag":
            had_restriction = True
            writer.u(1, 1)
            continue
        if name == "max_num_reorder_frames":
            # Discord requires 0: the stream carries no B-frames, so the
            # decoder must not buffer frames for reordering.
            writer.ue(0)
            continue
        if kind == "flag":
            writer.u(value, 1)
        elif kind == "ue":
            writer.ue(value)
        else:
            writer.u(value, width)

    if not had_restriction:
        # The VUI carried no restriction block: append a minimal one.
        writer.u(1, 1)  # bitstream_restriction_flag = 1
        writer.u(0, 1)  # motion_vectors_over_pic_boundaries_flag
        writer.ue(0)  # max_bytes_per_pic_denom
        writer.ue(0)  # max_bits_per_mb_denom
        writer.ue(0)  # log2_max_mv_length_horizontal
        writer.ue(0)  # log2_max_mv_length_vertical
        writer.ue(0)  # max_num_reorder_frames = 0
        writer.ue(0)  # max_dec_frame_buffering = 0


def rewrite_sps(sps_nal: bytes) -> bytes:
    """Return an SPS NAL whose VUI carries the bitstream restriction Discord needs.

    The input NAL must include its one byte NAL header. The original NAL is
    returned unchanged when it cannot be parsed safely.
    """
    if not sps_nal or len(sps_nal) < 4:
        return sps_nal
    if (sps_nal[0] & 0x1F) != NAL_SPS:
        return sps_nal

    header = sps_nal[0]
    try:
        rbsp = unescape_rbsp(sps_nal[1:])
        reader = BitReader(rbsp)
        _parse_sps_prefix(reader)
        prefix_bits = list(reader.consumed)
        vui_present = prefix_bits[-1] == 1

        fields = _parse_vui(reader) if vui_present else []

        writer = BitWriter(prefix_bits)
        if vui_present:
            _emit_vui(writer, fields)
        else:
            # No VUI at all: turn the flag on and append a minimal one.
            writer.bits[-1] = 1
            writer.u(0, 1)  # aspect_ratio_info_present_flag
            writer.u(0, 1)  # overscan_info_present_flag
            writer.u(0, 1)  # video_signal_type_present_flag
            writer.u(0, 1)  # chroma_loc_info_present_flag
            writer.u(0, 1)  # timing_info_present_flag
            writer.u(0, 1)  # nal_hrd_parameters_present_flag
            writer.u(0, 1)  # vcl_hrd_parameters_present_flag
            writer.u(0, 1)  # pic_struct_present_flag
            writer.u(1, 1)  # bitstream_restriction_flag
            writer.u(0, 1)  # motion_vectors_over_pic_boundaries_flag
            writer.ue(0)  # max_bytes_per_pic_denom
            writer.ue(0)  # max_bits_per_mb_denom
            writer.ue(0)  # log2_max_mv_length_horizontal
            writer.ue(0)  # log2_max_mv_length_vertical
            writer.ue(0)  # max_num_reorder_frames
            writer.ue(0)  # max_dec_frame_buffering

        writer.trailing()
        rebuilt = bytes([header]) + escape_rbsp(writer.to_bytes())
        return rebuilt
    except Exception:
        return sps_nal


def rewrite_frame(nalus: list[bytes]) -> list[bytes]:
    """Apply the SPS rewrite to a list of NAL units belonging to one frame."""
    out: list[bytes] = []
    for nal in nalus:
        if nal and (nal[0] & 0x1F) == NAL_SPS:
            out.append(rewrite_sps(nal))
        else:
            out.append(nal)
    return out
