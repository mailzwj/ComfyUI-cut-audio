from typing_extensions import override
import json

import torch
from comfy_api.latest import ComfyExtension, IO


class AudioInfo(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="AudioInfo_CutAudio",
            display_name="音频信息",
            description="获取音频的基本信息：时长、采样率和视频帧数。",
            category="audio/cut-audio",
            inputs=[
                IO.Audio.Input("audio"),
                IO.Float.Input(
                    "fps",
                    default=24.0,
                    min=1.0,
                    max=120.0,
                    step=0.01,
                    tooltip="视频帧率，用于计算音频对应的视频总帧数。",
                ),
            ],
            outputs=[
                IO.Float.Output(display_name="时长(秒)"),
                IO.Int.Output(display_name="采样率"),
                IO.Int.Output(display_name="总帧数"),
            ],
        )

    @classmethod
    def execute(cls, audio, fps) -> IO.NodeOutput:
        if audio is None:
            raise ValueError("AudioInfo: 输入音频为空。")
        waveform = audio["waveform"]
        sample_rate = audio["sample_rate"]
        num_samples = waveform.shape[-1]
        duration = num_samples / sample_rate
        frame_count = int(round(duration * fps))
        return IO.NodeOutput(duration, sample_rate, frame_count)


class AudioSplitBySilence(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="AudioSplitBySilence_CutAudio",
            display_name="按静音切分音频",
            description="根据静音间隔自动将音频切分为多个片段。",
            category="audio/cut-audio",
            inputs=[
                IO.Audio.Input("audio"),
                IO.Float.Input(
                    "silence_threshold_dB",
                    default=-40.0,
                    min=-80.0,
                    max=0.0,
                    step=1.0,
                    tooltip="静音阈值（dB）。低于此电平的帧视为静音。",
                ),
                IO.Int.Input(
                    "min_silence_duration_ms",
                    default=300,
                    min=50,
                    max=5000,
                    step=50,
                    tooltip="触发切分的最短静音时长（毫秒）。",
                ),
                IO.Int.Input(
                    "min_segment_duration_ms",
                    default=200,
                    min=50,
                    max=10000,
                    step=50,
                    tooltip="短于此时长（毫秒）的片段将被丢弃。",
                ),
            ],
            outputs=[
                IO.Audio.Output(display_name="音频片段"),
                IO.Int.Output(display_name="片段数量"),
                IO.String.Output(display_name="对齐信息"),
            ],
        )

    @classmethod
    def _single_segment_output(cls, audio, sample_rate, total_samples):
        duration = round(total_samples / sample_rate, 3)
        alignment = json.dumps([{"start": 0.0, "duration": duration}], ensure_ascii=False)
        return IO.NodeOutput(audio, 1, alignment)

    @classmethod
    def execute(cls, audio, silence_threshold_dB, min_silence_duration_ms, min_segment_duration_ms) -> IO.NodeOutput:
        if audio is None:
            raise ValueError("AudioSplitBySilence: 输入音频为空。")
        waveform = audio["waveform"]
        sample_rate = audio["sample_rate"]
        total_samples = waveform.shape[-1]

        wav = waveform[0]
        mono = wav.mean(dim=0)

        threshold_linear = 10 ** (silence_threshold_dB / 20.0)
        min_silence_samples = int(sample_rate * min_silence_duration_ms / 1000)
        min_segment_samples = int(sample_rate * min_segment_duration_ms / 1000)

        frame_size = max(1, sample_rate // 100)
        num_frames = mono.shape[0] // frame_size
        if num_frames == 0:
            return cls._single_segment_output(audio, sample_rate, total_samples)

        frames = mono[:num_frames * frame_size].reshape(num_frames, frame_size)
        frame_energy = frames.abs().max(dim=1).values
        is_silent = (frame_energy < threshold_linear).int()

        # Vectorized silence region detection
        padded_silent = torch.cat([torch.zeros(1, dtype=torch.int), is_silent, torch.zeros(1, dtype=torch.int)])
        diffs = torch.diff(padded_silent)
        starts = torch.where(diffs == 1)[0]
        ends = torch.where(diffs == -1)[0]
        lengths = ends - starts
        mask = (lengths * frame_size) >= min_silence_samples
        silent_regions = (((starts[mask] + ends[mask]) // 2) * frame_size).tolist()

        split_points = [0] + silent_regions + [total_samples]
        segments = []
        segment_starts = []
        for i in range(len(split_points) - 1):
            s = split_points[i]
            e = split_points[i + 1]
            if e - s >= min_segment_samples:
                segments.append(waveform[..., s:e])
                segment_starts.append(s)

        if not segments:
            return cls._single_segment_output(audio, sample_rate, total_samples)

        segment_lengths = [seg.shape[-1] for seg in segments]

        alignment = json.dumps(
            [
                {"start": round(s / sample_rate, 3), "duration": round(l / sample_rate, 3)}
                for s, l in zip(segment_starts, segment_lengths)
            ],
            ensure_ascii=False,
        )

        max_len = max(segment_lengths)
        padded = [torch.nn.functional.pad(seg, (0, max_len - seg.shape[-1])) for seg in segments]
        batched_waveform = torch.cat(padded, dim=0)
        result_audio = {
            "waveform": batched_waveform,
            "sample_rate": sample_rate,
            "segment_lengths": segment_lengths,
        }
        return IO.NodeOutput(result_audio, len(segments), alignment)


class AudioManualCut(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="AudioManualCut_CutAudio",
            display_name="手动裁剪音频",
            description="通过指定开始和结束时间（mm:ss格式）裁剪音频片段。",
            category="audio/cut-audio",
            inputs=[
                IO.Audio.Input("audio"),
                IO.String.Input(
                    "start_time",
                    default="00:00",
                    tooltip="开始时间，格式 mm:ss（如 01:30 表示1分30秒）。",
                ),
                IO.String.Input(
                    "end_time",
                    default="00:05",
                    tooltip="结束时间，格式 mm:ss（如 02:00 表示2分钟）。超出音频时长则裁剪到末尾。",
                ),
            ],
            outputs=[IO.Audio.Output(display_name="音频")],
        )

    @classmethod
    def _parse_time(cls, time_str: str) -> float:
        parts = time_str.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"时间格式错误 '{time_str}'，应为 mm:ss。")
        try:
            minutes = int(parts[0])
            seconds = int(parts[1])
        except ValueError:
            raise ValueError(f"时间格式错误 '{time_str}'，mm 和 ss 必须为整数。")
        if seconds >= 60 or seconds < 0 or minutes < 0:
            raise ValueError(f"时间值无效 '{time_str}'，秒数应为 0-59，分钟数 >= 0。")
        return minutes * 60.0 + seconds

    @classmethod
    def execute(cls, audio, start_time, end_time) -> IO.NodeOutput:
        if audio is None:
            raise ValueError("AudioManualCut: 输入音频为空。")
        waveform = audio["waveform"]
        sample_rate = audio["sample_rate"]
        total_samples = waveform.shape[-1]

        start_seconds = cls._parse_time(start_time)
        end_seconds = cls._parse_time(end_time)

        start_sample = int(round(start_seconds * sample_rate))
        end_sample = int(round(end_seconds * sample_rate))

        start_sample = max(0, min(start_sample, total_samples))
        end_sample = max(0, min(end_sample, total_samples))

        if start_sample >= end_sample:
            raise ValueError(
                f"AudioManualCut: 开始时间（{start_time}）必须早于结束时间（{end_time}），"
                f"且在音频时长（{total_samples / sample_rate:.2f}秒）范围内。"
            )

        cut_waveform = waveform[..., start_sample:end_sample]
        return IO.NodeOutput({"waveform": cut_waveform, "sample_rate": sample_rate})


class AudioSelectSegment(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="AudioSelectSegment_CutAudio",
            display_name="选取音频片段",
            description="通过索引从批量音频片段中选取单个片段。",
            category="audio/cut-audio",
            inputs=[
                IO.Audio.Input("audio_segments"),
                IO.Int.Input(
                    "index",
                    default=0,
                    min=0,
                    max=9999,
                    step=1,
                    tooltip="要提取的片段索引（从0开始）。",
                ),
            ],
            outputs=[IO.Audio.Output(display_name="音频")],
        )

    @classmethod
    def execute(cls, audio_segments, index) -> IO.NodeOutput:
        if audio_segments is None:
            raise ValueError("AudioSelectSegment: 输入音频片段为空。")
        waveform = audio_segments["waveform"]
        sample_rate = audio_segments["sample_rate"]
        segment_lengths = audio_segments.get("segment_lengths")

        batch_size = waveform.shape[0]
        if index >= batch_size:
            raise ValueError(
                f"AudioSelectSegment: 索引 {index} 超出范围（共 {batch_size} 个片段）。"
            )

        segment = waveform[index:index + 1]
        if segment_lengths and index < len(segment_lengths):
            actual_len = segment_lengths[index]
            segment = segment[..., :actual_len]
        return IO.NodeOutput({"waveform": segment, "sample_rate": sample_rate})


class CutAudioExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[IO.ComfyNode]]:
        return [
            AudioInfo,
            AudioSplitBySilence,
            AudioManualCut,
            AudioSelectSegment,
        ]


async def comfy_entrypoint() -> CutAudioExtension:
    return CutAudioExtension()


NODE_CLASS_MAPPINGS = {
    "AudioInfo_CutAudio": AudioInfo,
    "AudioSplitBySilence_CutAudio": AudioSplitBySilence,
    "AudioManualCut_CutAudio": AudioManualCut,
    "AudioSelectSegment_CutAudio": AudioSelectSegment,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "AudioInfo_CutAudio": "音频信息",
    "AudioSplitBySilence_CutAudio": "按静音切分音频",
    "AudioManualCut_CutAudio": "手动裁剪音频",
    "AudioSelectSegment_CutAudio": "选取音频片段",
}
