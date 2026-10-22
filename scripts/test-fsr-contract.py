#!/usr/bin/env python3
"""Exercise the DXMT-derived FSR SR contract without requiring Metal."""

import pathlib
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]

HARNESS = r"""
#include "d3dmetal-sidecar/fsr-contract.hpp"
#include <cassert>
#include <cmath>

using namespace yaagl::pso::fsr;

static yaagl_fsr_create_packet create_packet(std::uint32_t flags = 0) {
    yaagl_fsr_create_packet packet{};
    packet.device = 1;
    packet.flags = flags;
    packet.max_render_width = 640;
    packet.max_render_height = 360;
    packet.max_upscale_width = 1280;
    packet.max_upscale_height = 720;
    return packet;
}

static yaagl_fsr_dispatch_packet frame_packet() {
    yaagl_fsr_dispatch_packet packet{};
    packet.command_list = 1;
    packet.color = 2;
    packet.depth = 3;
    packet.motion_vectors = 4;
    packet.output = 5;
    packet.color_state = packet.depth_state = packet.motion_state = packet.output_state = 1;
    packet.render_width = 640;
    packet.render_height = 360;
    packet.upscale_width = 1280;
    packet.upscale_height = 720;
    packet.motion_scale_x = packet.motion_scale_y = 1.0f;
    packet.pre_exposure = 1.0f;
    packet.camera_near = 0.1f;
    packet.camera_far = 1000.0f;
    packet.camera_fov_vertical = 1.0f;
    return packet;
}

int main() {
    auto create = create_packet();
    CreateContract contract;
    assert(validateCreate(create, contract) == ContractStatus::Ok);
    assert(contract.backend.dynamicOutput);

    auto frame = frame_packet();
    FrameContract result;
    assert(validateFrame(contract, frame, result) == ContractStatus::Ok);
    assert(result.backend.compositionRect.width == frame.render_width);

    frame.motion_scale_x = 0.0f;
    assert(validateFrame(contract, frame, result) == ContractStatus::InvalidParameter);

    frame = frame_packet();
    frame.camera_far = INFINITY;
    assert(validateFrame(contract, frame, result) == ContractStatus::InvalidParameter);
    create = create_packet(1u << 4);
    assert(validateCreate(create, contract) == ContractStatus::Ok);
    assert(validateFrame(contract, frame, result) == ContractStatus::Ok);

    create = create_packet();
    assert(validateCreate(create, contract) == ContractStatus::Ok);
    frame = frame_packet();
    frame.flags = 1u;
    assert(validateFrame(contract, frame, result) == ContractStatus::InvalidParameter);
    create = create_packet(1u << 9);
    assert(validateCreate(create, contract) == ContractStatus::Ok);
    assert(validateFrame(contract, frame, result) == ContractStatus::Ok);
    assert(result.operations.drawDebugView);

    frame = frame_packet();
    frame.color_state = (1u << 1) | (1u << 2);
    assert(validateFrame(contract, frame, result) == ContractStatus::InvalidParameter);
    return 0;
}
"""


def main():
    with tempfile.TemporaryDirectory() as directory:
        directory = pathlib.Path(directory)
        source = directory / "test.cpp"
        binary = directory / "test"
        source.write_text(HARNESS)
        subprocess.run(
            [
                "clang++",
                "-std=c++20",
                "-O2",
                "-iquote",
                str(ROOT),
                str(source),
                str(ROOT / "d3dmetal-sidecar/fsr-contract.cpp"),
                "-o",
                str(binary),
            ],
            check=True,
        )
        subprocess.run((str(binary),), check=True)
    print("FSR_CONTRACT_PASS")


if __name__ == "__main__":
    main()
