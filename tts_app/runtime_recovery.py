from __future__ import annotations

from .runtime_core import *
from .runtime_audio import *
from .runtime_storage import RECOVERY_DIR

def cleanup_legacy_temp_workdirs() -> int:
    """Delete old pre-2.9 temp folders that can no longer be resumed safely."""
    removed = 0
    cutoff = time.time() - RECOVERY_RETENTION_DAYS * 86400
    temp_root = Path(tempfile.gettempdir())
    try:
        candidates = list(temp_root.glob("text_to_mp3_*"))
    except Exception:
        return 0

    for path in candidates:
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except Exception:
            pass
    return removed


def scan_recovery_jobs() -> tuple[list[dict], int]:
    """
    Return resumable job manifests and remove expired/corrupt recovery folders.
    Live jobs owned by any running process are never touched.
    """
    RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
    jobs: list[dict] = []
    removed = 0
    cutoff = time.time() - RECOVERY_RETENTION_DAYS * 86400

    for job_dir in RECOVERY_DIR.glob("job_*"):
        if not job_dir.is_dir():
            continue

        manifest_path = job_dir / "recovery.json"
        try:
            payload = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            if not isinstance(payload, dict):
                raise ValueError("recovery.json is not an object")

            run_id = str(payload.get("run_id") or "").strip()
            text_sha256 = str(
                payload.get("text_sha256") or ""
            ).strip()
            chunks_total = clamp_int(
                payload.get("chunks_total"),
                0,
                10_000_000,
                0,
            )
            output_text = str(
                payload.get("output_path") or ""
            ).strip()

            if (
                not run_id
                or not text_sha256
                or chunks_total <= 0
                or not output_text
            ):
                raise ValueError(
                    "recovery.json misses required fields"
                )

            owner_pid = clamp_int(
                payload.get("owner_pid"),
                0,
                2_147_483_647,
                0,
            )
            if owner_pid and process_is_running(owner_pid):
                continue

            if job_dir.stat().st_mtime < cutoff:
                output_path = Path(output_text)
                partial_output = output_path.with_name(
                    output_path.stem + ".part.mp3"
                )
                try:
                    partial_output.unlink(missing_ok=True)
                except OSError:
                    pass
                shutil.rmtree(job_dir, ignore_errors=True)
                removed += 1
                continue

            payload["_job_dir"] = str(job_dir)
            payload["_manifest_path"] = str(manifest_path)
            jobs.append(payload)

        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            removed += 1

    jobs.sort(
        key=lambda item: str(item.get("updated_at") or ""),
        reverse=True,
    )
    return jobs, removed


def count_valid_recovery_wavs(
    work_dir: Path,
    chunks_total: int,
) -> tuple[int, int, float, dict]:
    """
    Count only a consecutive valid prefix: chunk_00001.wav ... chunk_N.wav.
    Any broken/later stale files are removed so a resume cannot mix old data.
    """
    valid_count = 0
    total_bytes = 0
    total_audio_sec = 0.0
    first_sample: dict = {}

    for index in range(1, chunks_total + 1):
        path = work_dir / f"chunk_{index:05d}.wav"
        try:
            if not path.exists() or path.stat().st_size < 128:
                break
            info = get_wav_info(path)
            if not info or not info.get(
                "appears_complete",
                False,
            ):
                break
            valid_count = index
            total_bytes += int(path.stat().st_size)
            total_audio_sec += float(info.get("duration_sec") or 0)
            if not first_sample:
                first_sample = {
                    "sample_chunk_index": index,
                    **info,
                }
        except Exception:
            break

    for path in work_dir.glob("chunk_*.wav"):
        match = re.fullmatch(r"chunk_(\d{5})\.wav", path.name)
        if not match:
            continue
        if int(match.group(1)) > valid_count:
            try:
                path.unlink()
            except OSError:
                pass

    return valid_count, total_bytes, total_audio_sec, first_sample
