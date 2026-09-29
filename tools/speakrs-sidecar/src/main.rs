//! Recap 전용 CoreML sidecar: recap-speakrs INPUT.wav OUTPUT.json
use speakrs::{ExecutionMode, OwnedDiarizationPipeline};
use std::{collections::HashSet, env, error::Error, fs::File};

fn main() -> Result<(), Box<dyn Error + Send + Sync>> {
    let args: Vec<_> = env::args_os().collect();
    if args.len() != 3 {
        return Err("usage: recap-speakrs INPUT.wav OUTPUT.json".into());
    }
    if !cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        return Err("speakrs requires an Apple Silicon Mac".into());
    }
    // 모델 다운로드/로드 전에 열린 WAV handle로 입력을 고정한다.
    let mut reader = hound::WavReader::open(&args[1])?;
    let spec = reader.spec();
    if spec.channels != 1 || spec.sample_rate != 16000 || spec.bits_per_sample != 16
        || spec.sample_format != hound::SampleFormat::Int
    {
        return Err("expected 16 kHz mono PCM16 WAV".into());
    }
    let audio: Vec<f32> = reader.samples::<i16>()
        .map(|sample| sample.map(|v| f32::from(v) / 32768.0))
        .collect::<Result<_, _>>()?;
    let mut pipeline = OwnedDiarizationPipeline::from_pretrained(ExecutionMode::CoreMl)?;
    let result = pipeline.run(&audio)?;
    let segments: Vec<_> = result.discrete_diarization.to_segments().into_iter()
        .map(|segment| serde_json::json!({
            "speaker": segment.speaker,
            "start": segment.start,
            "end": segment.end,
        })).collect();
    let speakers: HashSet<_> = segments.iter().map(|s| s["speaker"].as_str().unwrap()).collect();
    let output = serde_json::json!({
        "segments": segments,
        "num_speakers": speakers.len(),
        "audio_path": args[1].to_string_lossy(),
        "model_name": "speakrs-coreml",
        "output_mode": "regular",
    });
    serde_json::to_writer(File::create(&args[2])?, &output)?;
    Ok(())
}
