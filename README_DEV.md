# Інструкція для розробника

## 1. Запуск тестування

1. Встановіть залежності для розробки:
   ```bash
   pip install -r requirements.txt
   ```

2. Запустіть тести:
   ```bash
   python -m pytest
   # Або для детального відображення логів
   python -m pytest -qo log_cli=true --log-cli-level=DEBUG -o log_level=DEBUG
   ```
   (Тести мають бути у папці `tests/` і починатися з `test_*.py`.)

3. Увімкніть git hook, який запускає тести перед кожним `git push` і скасовує push, якщо тести падають:
   ```bash
   git config core.hooksPath .githooks
   ```
   Хук проганяє тести двічі: на `venv/` (інакше на `python` з оточення) і на мінімальних версіях — Python 3.10, pydantic 2.11.0.
   Для другого прогону потрібен [uv](https://docs.astral.sh/uv/): він сам завантажить Python 3.10.
   Разово обійти перевірку: `git push --no-verify`.

---

## 2. Збірка пакету

```bash
make build   # чиста збірка: dist/ з .whl і .tar.gz
make check   # збірка + `twine check --strict`: метадані й README, як їх побачить PyPI
```

Потрібні `build` і `twine` у venv: `pip install --upgrade build twine`.

---

## 3. Реліз: GitHub Release → PyPI

Реліз — це тег `vX.Y.Z`, GitHub Release на ньому й пакет на PyPI, зібраний з того самого коміту.
Публікацію GitHub Release підхоплює workflow `.github/workflows/release.yml`: тести (Python 3.10 + pydantic 2.11.0 і Python 3.13 + поточна pydantic) → збірка й `twine check --strict` → завантаження на PyPI.
Токена немає ніде: PyPI довіряє саме цьому workflow через Trusted Publishing.

### Одноразове налаштування

1. **PyPI**: [pypi.org](https://pypi.org/) → Your projects → `elasticmodel` → Manage → Publishing → Add a new publisher → GitHub:
   - Owner: `Steppe-Mammoth`
   - Repository name: `ElasticModel`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. **GitHub**: репозиторій → Settings → Environments → New environment `pypi`.
   За бажанням — Required reviewers: тоді завантаження на PyPI чекатиме вашого підтвердження у вкладці Actions.

### Кожен реліз

1. Злийте зміни в `master`.
2. Підніміть версію в `pyproject.toml` і додайте її розділ у `CHANGELOG.md`, закомітьте й запуште в `master`.
   Опубліковану версію PyPI вдруге не прийме навіть після видалення.
3. За бажанням перевірте збірку на [TestPyPI](https://test.pypi.org/): `make publish-test` (окремий акаунт і токен).
4. GitHub → Releases → Draft a new release:
   - Choose a tag: `v0.4.0` (нова версія з `pyproject.toml`) → Create new tag on publish, target `master`;
   - заголовок `v0.4.0`, опис — головне з `CHANGELOG.md` (Generate release notes підставить список PR);
   - Publish release.
5. Стежте за workflow `release` у вкладці Actions. Пакет з'явиться на <https://pypi.org/p/elasticmodel>.

Якщо workflow впав до кроку публікації, на PyPI нічого не потрапило: виправте причину і перезапустіть невдалі завдання (Re-run failed jobs).

### Запасний шлях: вручну з машини

```bash
make publish   # тести, збірка, перевірка, підтвердження й завантаження на PyPI
```

Тег і GitHub Release тоді створюються окремо вручну.
Токен у Makefile не зберігається, twine бере його з одного з джерел:
- змінні оточення `TWINE_USERNAME=__token__` і `TWINE_PASSWORD=pypi-...`;
- `~/.pypirc` з `username = __token__` і `password = pypi-...`;
- keyring: `python -m keyring set https://upload.pypi.org/legacy/ __token__`;
- інакше питає логін і токен у терміналі (логін — `__token__`). 