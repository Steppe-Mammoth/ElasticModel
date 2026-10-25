# Збірка й публікація пакета на PyPI вручну.
# Основний шлях релізу — GitHub Release і workflow `.github/workflows/release.yml` (README_DEV.md, розділ 3); `make publish` — запасний.
# Токен PyPI тут не зберігається: twine бере його з `TWINE_PASSWORD` (разом з `TWINE_USERNAME=__token__`), `~/.pypirc` або keyring, а інакше питає в терміналі.
#
#   make test          — тести, як у pre-push hook: venv і мінімальні версії (Python 3.10, pydantic 2.11.0)
#   make build         — чиста збірка wheel і sdist у dist/
#   make check         — збірка й перевірка метаданих і README для PyPI
#   make publish-test  — тести, перевірка й завантаження на TestPyPI
#   make publish       — тести, перевірка, підтвердження й завантаження на PyPI

# Інтерпретатор з venv проєкту, якщо він є, — так само, як у pre-push hook.
PYTHON := $(if $(wildcard venv/bin/python),venv/bin/python,python)
VERSION := $(shell $(PYTHON) -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")

.PHONY: test clean build check publish-test publish

# Той самий скрипт, що й pre-push hook, щоб перелік прогонів жив в одному місці.
test:
	sh .githooks/pre-push

# Старі артефакти прибираються, бо `twine upload dist/*` завантажив би й файли попередніх версій.
clean:
	rm -rf dist build elasticmodel.egg-info

build: clean
	$(PYTHON) -m build

# `--strict` робить попередження помилками: README, який PyPI не відрендерить, зупиняє публікацію.
check: build
	$(PYTHON) -m twine check --strict dist/*

publish-test: test check
	$(PYTHON) -m twine upload --repository testpypi dist/*

# Публікація незворотна: ту саму версію PyPI не прийме вдруге навіть після видалення, тому перед завантаженням — підтвердження.
publish: test check
	@printf "Опублікувати elasticmodel $(VERSION) на PyPI? [y/N] "; read answer; [ "$$answer" = "y" ] || { echo "Скасовано"; exit 1; }
	$(PYTHON) -m twine upload dist/*
