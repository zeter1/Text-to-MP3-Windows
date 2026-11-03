"""Private JSONL worker. Never import the application or print text/service exceptions."""
import array
import asyncio
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
import wave

if __package__:
    from .voice_catalog import voice_spec, verified_model, EDGE_RUNTIME, MODEL_PATH
else:
    from voice_catalog import voice_spec, verified_model, EDGE_RUNTIME, MODEL_PATH

ERRORS = {
    'dependency': 'Не установлен движок. Выполните: python tools/setup_voices.py --install-deps',
    'model': 'Локальная модель отсутствует или повреждена. Выполните: python tools/setup_voices.py',
    'synthesis': 'Движок не смог озвучить фрагмент. Для Edge проверьте интернет; попробуйте другой голос.',
    'protocol': 'Некорректный запрос к движку речи.',
}


def main() -> None:
    optional = MODEL_PATH.parent / 'optional_dependencies'
    if optional.is_dir():
        sys.path.append(str(optional))
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    spec = voice_spec(sys.argv[1]) if len(sys.argv) == 2 else None
    model = None
    torch = None
    edge_tts = None
    while True:
        line = sys.stdin.readline(262145)
        if not line:
            return
        request_id = None
        code = 'protocol'
        try:
            request = json.loads(line)
            request_id = request['id']
            if (not spec or len(line) > 262144 or not isinstance(request_id, int)
                    or not isinstance(request.get('text'), str) or not 0 < len(request['text']) <= 1000
                    or not isinstance(request.get('path'), str)):
                raise ValueError('protocol')
            backend, speaker = spec
            target = Path(request['path'])
            code = 'dependency'
            # Model/library stdout is not the protocol and may contain source text.
            with redirect_stdout(sys.stderr):
                if backend == 'silero':
                    if torch is None:
                        import torch as torch_module
                        torch = torch_module
                        torch.set_num_threads(2)
                    if model is None:
                        code = 'model'
                        with verified_model() as source:
                            model = torch.package.PackageImporter(source).load_pickle('tts_models', 'model')
                        model.to(torch.device('cpu'))
                    code = 'synthesis'
                    with torch.inference_mode():
                        audio = model.apply_tts(text=request['text'], speaker=speaker, sample_rate=24000)
                    samples = array.array('h', audio.clamp(-1, 1).mul(32767).to(torch.int16).tolist())
                    if sys.byteorder != 'little':
                        samples.byteswap()
                    with wave.open(str(target), 'wb') as output:
                        output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                        output.writeframes(samples.tobytes())
                else:
                    if edge_tts is None:
                        if not (EDGE_RUNTIME / 'edge_tts' / '__init__.py').is_file():
                            raise RuntimeError('dependency')
                        sys.path.insert(0, str(EDGE_RUNTIME))
                        import edge_tts as edge_module
                        edge_tts = edge_module
                    code = 'synthesis'
                    asyncio.run(edge_tts.Communicate(request['text'], speaker,
                                                     connect_timeout=10, receive_timeout=30).save(str(target)))
            response = {'id': request_id, 'ok': True}
        except Exception:
            response = {'id': request_id, 'ok': False, 'code': code, 'message': ERRORS[code]}
        print(json.dumps(response, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
