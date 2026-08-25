# mdlink

CLI-проверка ссылок в Markdown: находит `.md`-файлы, извлекает ссылки через AST
CommonMark и проверяет каждую — локальные на существование файла и якоря,
внешние на HTTP-доступность. Реализация [технической спецификации](./spec.md) v1.0.

```
mdlink [PATH] [OPTIONS]
```

## Установка

```bash
pip install -e ".[dev]"
```

Python 3.11+. Зависимости: `markdown-it-py`, `httpx[http2]`, `rich`, `typer`, `pathspec`.
Флаг `--bare-urls` дополнительно требует `linkify-it-py` (`pip install -e ".[bare-urls]"`).

## Быстрый старт

```bash
mdlink .
```

```bash
mdlink . --format json --no-external
```

```bash
mdlink --repo owner/name@main
```

## Что проверяется

| Вид ссылки | Пример | Как проверяется |
|---|---|---|
| Локальный путь | `[a](./docs/api.md)` | существование файла от директории **текущего** `.md` |
| Корневой путь | `[a](/docs/api.md)` | от `--root` (корня проекта), **не** от корня ФС |
| Якорь | `[a](./api.md#install)` | slug-и заголовков по алгоритму GitHub + явные `id`/`name`/`{#custom}` |
| Изображение | `![alt](./img.png)` | наравне со ссылками (`--no-check-images` отключает) |
| Внешний URL | `<https://example.com>` | `HEAD`, при `403/405/501` — `GET` с `Range: bytes=0-0` |
| `file://` | `file:///tmp/a.md` | существование + предупреждение о непереносимости |
| Wiki-ссылка | `[[Работа с Git]]` | поиск по basename внутри `--root` (`--wikilinks`) |

Ссылки внутри fenced- и indented-блоков, инлайн-кода и HTML-комментариев не
извлекаются **никогда** — парсинг идёт по AST, а вспомогательные регулярки
работают по тексту с вырезанными блоками кода.

## Ключевые решения реализации

Места, где утилиты такого рода ломаются чаще всего, и как здесь сделано:

- **База относительных ссылок** — `source_file.parent`, не CWD и не корень проекта.
  Прогон из любой директории даёт идентичный результат.
- **Регистр имён проверяется явно** через `os.listdir`, а не через `exists()`:
  иначе `./ReadMe.md` пройдёт на macOS и упадёт на GitHub. Несовпадение →
  `case_mismatch` с реальным именем в `suggestion`.
- **Unicode-нормализация.** APFS/HFS+ хранят имена в NFD (`й` = `и` + U+0306),
  редакторы отдают NFC. Сравнение всегда идёт после `unicodedata.normalize("NFC", …)`
  с обеих сторон, регистронезависимое — через `casefold()`, а не `lower()`.
  К файловой системе утилита обращается **только** по исходной строке из `listdir`;
  нормализованная форма живёт в сравнениях и в выводе.
- **Неэкранированные пробелы.** По CommonMark `[текст](Работа с Git.md)` — не ссылка,
  и молчаливое отсутствие её в отчёте хуже ложного срабатывания. Второй проход
  ловит такую запись, отдаёт `WARNING: unencoded_space` с корректным вариантом в
  `suggestion` и при этом проверяет цель на существование.
- **Резолвинг без обращения к ФС** — `os.path.normpath`, а не `Path.resolve()`:
  последний разыменовал бы симлинки и сделал невозможной проверку `broken_symlink`.
- **Сеть.** Заданы connect- и read-таймауты и внешний дедлайн вокруг всей
  retry-обёртки. Ретраи — только на сетевые ошибки, `429` и `5xx` кроме `501`.
  Лимит запросов и глобальный, и на хост. Одинаковые URL запрашиваются один раз.
- **Приватные хосты** (`localhost`, `127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`,
  `169.254/16`, `*.local`, `*.internal`) по умолчанию не запрашиваются вовсе:
  в инструкциях «Локальный запуск» это честный `SKIPPED`, а не 12 ложных
  `connection_error`. Снимается флагом `--allow-private-hosts`.

## Опции

### Источник

| Флаг | Дефолт | Описание |
|---|---|---|
| `PATH` | `.` | Папка или отдельный `.md`-файл. Взаимоисключим с `--repo` |
| `--repo owner/name[@ref]` | — | Скачать и проверить GitHub-репозиторий |
| `--root PATH` | = `PATH` | Корень для резолвинга `/абсолютных` ссылок |
| `--include TEXT` | `**/*.md`, `**/*.markdown` | Глоб сканируемых файлов, многократный |
| `--exclude TEXT` | `node_modules`, `.git`, `venv`, `dist`, `build`, `vendor`, `.next`, `target` | Глоб исключений, многократный |
| `--no-gitignore` | false | Не учитывать `.gitignore` (включая вложенные) |
| `--follow-symlinks` | false | Ходить по симлинкам директорий |

### Что проверять

| Флаг | Дефолт | Описание |
|---|---|---|
| `--no-external` | false | Пропустить HTTP(S)-ссылки |
| `--no-local` | false | Пропустить локальные ссылки |
| `--check-anchors / --no-check-anchors` | true | Проверять `#якоря` |
| `--check-images / --no-check-images` | true | Проверять `![alt](src)` |
| `--no-dir-index` | false | Не считать `README.md`/`index.md` валидной целью ссылки на директорию |
| `--wikilinks` | авто | Разбирать `[[...]]`; включается сам при наличии `.obsidian/` |
| `--bare-urls` | false | Проверять голые URL в тексте |
| `--ignore-url TEXT` | — | Regex; совпавшие URL → `SKIPPED`, многократный |
| `--allow-private-hosts` | false | Разрешить `localhost` и приватные IP |

### Сеть

| Флаг | Дефолт |
|---|---|
| `--timeout FLOAT` | `10.0` |
| `--connect-timeout FLOAT` | `5.0` |
| `--retries INT` | `2` |
| `--concurrency INT` | `16` |
| `--per-host INT` | `4` |
| `--max-redirects INT` | `5` |
| `--user-agent TEXT` | `mdlink/1.0 (+…)` |
| `--insecure` | false |
| `--github-token TEXT` | `$GITHUB_TOKEN` |

### Вывод

| Флаг | Дефолт | Описание |
|---|---|---|
| `--format` | `pretty` | `pretty` \| `json` \| `markdown` \| `junit` |
| `--output PATH` | stdout | Куда писать отчёт |
| `--all` | false | Показывать и живые ссылки |
| `--quiet`, `-q` | false | Только итоговая сводка |
| `--verbose`, `-v` | 0 | `-v` — предупреждения, `-vv` — лог каждого шага |
| `--no-color` | авто | Также уважает `NO_COLOR`, `TERM=dumb` и не-TTY stdout |
| `--fail-on` | `error` | `error` \| `warning` \| `never` |
| `--config PATH` | см. ниже | Путь к конфигу |
| `--cache PATH` | — | Файл персистентного кэша результатов |

## Коды выхода

| Код | Значение |
|---|---|
| `0` | Нарушений уровня `--fail-on` нет |
| `1` | Найдены сломанные ссылки (или warning'и при `--fail-on warning`) |
| `2` | Ошибка использования: невалидные аргументы, путь не существует, битый конфиг |
| `3` | Внутренняя ошибка (traceback при `-vv`) |
| `130` | Прервано `Ctrl+C` — печатается частичный отчёт, временные файлы удаляются |

## Коды результатов

Публичный контракт: по кодам фильтруют в CI. Добавлять новые можно,
переименовывать — только с мажорной версией.

**BROKEN** — `file_not_found`, `broken_symlink`, `case_mismatch`, `empty_link`,
`undefined_reference`, `wikilink_not_found`, `not_found`, `client_error`,
`server_error`, `timeout`, `dns_error`, `connection_error`, `ssl_error`,
`too_many_redirects`

**WARNING** — `anchor_not_found`, `link_to_directory`, `outside_root`,
`permission_denied`, `absolute_file_url`, `unencoded_space`, `wikilink_ambiguous`,
`auth_required`, `rate_limited`, `insecure_redirect`, `file_too_large`

**SKIPPED** — `private_host`, `unsupported_scheme`, `ignored_by_pattern`,
`host_unreachable`, `external_disabled`, `local_disabled`, `cached`

**INFO** (только при `-v`, на код выхода не влияет) — `unicode_nfd_filename`

## Конфигурация

Поиск: `--config` → `./.mdlink.toml` → `./pyproject.toml` (секция `[tool.mdlink]`)
→ `~/.config/mdlink/config.toml`. Побеждает первый найденный, слияния между
файлами нет. Приоритет: **флаги CLI > переменные `MDLINK_*` > конфиг > дефолты**.

```toml
[tool.mdlink]
include = ["docs/**/*.md", "README.md"]
exclude = ["**/CHANGELOG.md"]
timeout = 15.0
retries = 3
concurrency = 8
check-anchors = true
ignore-url = ["^https://twitter\\.com/"]
fail-on = "error"
```

Невалидный конфиг (неизвестный ключ, неверный тип) → exit `2` с указанием
ключа и строки. Готовый пример — [.mdlink.toml.example](./.mdlink.toml.example).

## Интеграция с CI

```yaml
- run: pip install mdlink
- run: mdlink . --format json --no-external --output mdlink.json
```

Для комментария к PR подойдёт `--format markdown`, для GitLab CI / Jenkins —
`--format junit`.

## Архитектура

```
mdlink/
├── __main__.py       точка входа
├── cli.py            typer-команда, слияние с конфигом, коды выхода   §2
├── config.py         загрузка и валидация конфига                     §10
├── discovery.py      обход ФС, gitignore, include/exclude             §4
├── parser.py         markdown-it → list[Link]                         §5
├── engine.py         оркестрация прогона
├── checkers/
│   ├── local.py      резолвинг путей, регистр, Unicode, симлинки      §6.1–6.4
│   ├── anchors.py    slug-генерация, кэш заголовков                   §6.5
│   └── http.py       async-клиент, ретраи, семафоры, кэш              §7
├── models.py         Link, Result, Status, LinkKind                   §3
├── report/           pretty / json / markdown / junit                 §9
├── repo.py           режим --repo                                     §11
└── textutil.py       NFC, casefold, Левенштейн, усечение
```

Правила слоёв: `parser.py` не знает о ФС и сети, `checkers/*` ничего не печатают,
`report/*` не выполняют проверок. Между слоями ходят только dataclass'ы из
`models.py` — поэтому парсер тестируется на строках, а чекеры на fixture-директориях,
без запуска CLI.

## Тесты

```bash
pytest -q
```

```bash
coverage run --source=mdlink -m pytest -q && coverage report
```

222 теста покрывают все обязательные кейсы §13.2. Сеть не используется:
HTTP-сценарии идут через `respx`. Fixture-проект — `tests/fixtures/sample-project/`
с кириллическим разделом, файлами со спецсимволами и намеренно сломанными случаями.

Unicode-тесты (15a–15h) обязательны к прогону на macOS: только там NFD-имена
возникают естественным образом, и именно там ломается побайтовое сравнение.
CI-матрица включает Ubuntu, macOS и Windows на Python 3.11–3.13.

## Известные ограничения

- Неопределённые **shortcut**-ссылки (`[метка]` без `[метка]: …`) не помечаются
  `undefined_reference`: в CommonMark такая запись неотличима от обычного текста
  в квадратных скобках, и ложные срабатывания на `[x]`, `[ ]`, `[1]` были бы
  массовыми. Полные и collapsed-формы (`[текст][метка]`, `[метка][]`) проверяются.
- Fallback `HEAD → GET` срабатывает на статусы `403/405/501` и протокольные
  ошибки, но не на таймауты и отказы соединения: повторный `GET` упёрся бы в ту
  же стену и лишь удвоил длительность прогона.
- Попадание в персистентный кэш воспроизводит сохранённый статус (сломанная
  ссылка остаётся сломанной) и помечается заметкой `cached`, а не превращается
  в `SKIPPED` — иначе кэш прятал бы поломки от CI.
- Проверяется только статус-код, не содержимое страницы; краулинг внешних
  сайтов и автоисправление ссылок (`--fix`) в scope не входят.
