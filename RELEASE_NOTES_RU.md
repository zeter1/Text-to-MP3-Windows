# Изменения Windows-сборки

- Готовая Windows-сборка Text to MP3 теперь публикуется как постоянный GitHub Release.
- Portable ZIP содержит `Text-to-MP3-Windows.exe` и обязательную соседнюю папку `runtime`.
- Перед публикацией CI выполняет compile, offline regression tests, проверку SAPI/FFmpeg contracts, сборку EXE и packaged smoke-test.
- В релиз добавлен отдельный SHA-256 portable-архива.
- README теперь прямо указывает, где скачать готовую Windows-версию и как правильно запускать packaged-дистрибутив.

## Проверка

CI подтверждает packaged startup/self-test на GitHub-hosted Windows runner. Реальное воспроизведение через пользовательские SAPI-голоса, аудиоустройства и длительная MP3-конвертация требуют runtime-проверки на целевой Windows-машине.
