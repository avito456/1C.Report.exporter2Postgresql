"""Файловый монитор для загрузки таблиц из TXT-выгрузок отчётов 1С в PostgreSQL (DWH).

Файл TXT формируется отчётом 1С по событию рассылки в текстовый файл.
Конвейер: watchdog (app.server) -> разбор TXT (app.parsers.report_loader)
-> настройки из TOML (app.settings.tables_settings) -> загрузка в БД (app.db.db_uploader).
"""
