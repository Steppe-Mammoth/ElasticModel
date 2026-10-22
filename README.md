## ElasticModel

ElasticModel is a wrapper around `pydantic.BaseModel`(v2) designed to simplify working with partial (projected) data from databases or APIs.

**Key advantage:** nested models (“model-in-model”) work even when not all required fields are loaded. Methods remain usable at any level and operate on the data that **is** loaded.

> Pydantic’s `model_validate()` requires all required fields. `model_construct()` allows partials but keeps nested dicts as raw dicts. ElasticModel combines the best of both.

**Core features of ElasticModel:**
- ✅ Inherits the behavior of `pydantic.BaseModel`: code written for `BaseModel` keeps working after you replace it with `ElasticModel`, and the new features come as separate `elastic_*` methods. The few deliberate differences are listed in [Differences from BaseModel](#differences-from-basemodel).
- ✅ Allows creating models from incomplete data, providing full access — read fields, call methods, even on nested models.
- ✅ Avoids the need for declaring a bunch of `Optional` fields.
- ✅ Supports dynamic unknown fields — stored in `self.elastic_extra`, a dictionary for unprocessed data.
- ✅ Ensures strict access: only loaded fields can be accessed. Trying to access an unloaded field will raise `NotLoadedFieldError`.
- ✅ Supports recursive creation of nested models.
- ✅ Provides "shallow" or "deep" checks for required fields at any time (on demand).

ElasticModel combines the best of `BaseModel.model_validate` (structured/nested models) and `BaseModel.model_construct` (creation without validation), adding new flexibility for working with partial data — without overloading your codebase with `Optional` fields.

---
<details>
<summary>🌐Переклад тут🔱 ElasticModel — це `pydantic.BaseModel`(v2), який надає ... та спрощує...</summary>
... спрощує роботу з частковими (проекційними) даними з баз даних або API.


**Основна перевага — підтримка вкладених моделей ("модель у моделі") без необхідності завантаження всіх полів. Незважаючи на це, методи моделі залишаються доступними на всіх рівнях і працюють із тими даними, які були завантажені.**

⚠️*У Pydantic така поведінка була б неможливою, оскільки він вимагає наявності всіх полів*.

Основні можливості ElasticModel:
- ✅ Наслідує поведінку `pydantic.BaseModel`: код, написаний під `BaseModel`, працює без змін після заміни на `ElasticModel`, а нове доступне окремими методами `elastic_*`. Свідомі відмінності перелічені в розділі [Differences from BaseModel](#differences-from-basemodel).
- ✅ Дозволяє створювати моделі з неповними даними й отримувати повний доступ — читати поля, викликати методи, навіть на вкладених моделях.
- ✅ Не потребує великої кількості полів типу `Optional`.
- ✅ Підтримує динамічні (неописані) поля — вони зберігаються в `self.elastic_extra`, словнику для необроблених даних.
- ✅ Гарантує контроль доступу: можна звертатись лише до завантажених полів. Спроба доступу до незавантаженого поля викличе `NotLoadedFieldError`.
- ✅ Підтримує рекурсивне створення вкладених моделей.
- ✅ Дозволяє виконувати поверхневу або глибоку перевірку обов’язкових полів у будь-який момент (на вимогу користувача).

ElasticModel поєднує найкраще з `BaseModel.model_validate` (структуровані/вкладені моделі) та `BaseModel.model_construct` (створення без валідації), додаючи нову гнучкість у роботі з частковими даними — без перевантаження кодової бази полями `Optional`.

</details>
---

## Install
```bash
pip install elasticmodel
```
https://pypi.org/project/elasticmodel/

Requires Python 3.11+.
Python 3.10 is not blocked, but it is not supported. Known issue on 3.10: a model name written as a string inside a builtin generic is not resolved, so `elastic_create` raises `PydanticUserError`:

```python
class Node(ElasticModel):
    name: str
    children: list["Node"] = []   # Python 3.10: elastic_create -> PydanticUserError
```

On 3.10 use `List["Node"]` from `typing`, or put `from __future__ import annotations` at the top of the module.

---

## Quick start
**💡 `ElasticModel.elastic_create` validation modes**
> - **`validate=True`** - All values are coerced to their declared types and validated by Pydantic. The only exception is nested `ElasticModel`, which are built with support for "partial data".
> - **`validate=False`** - No coercion or validation is applied — values are kept *as-is*. The only exceptions is nested `BaseModel` and `ElasticModel`, which are built with support for "partial data".

```python
>>> from typing import Annotated
>>> from datetime import datetime
>>> from pydantic import Field, EmailStr
>>> from elasticmodel import ElasticModel, NotLoadedFieldError
>>> class Created(ElasticModel):
...     at: str
...     by: str
...     def datetime_from_at(self) -> datetime:
...         return datetime.strptime(self.at, "%Y-%m-%d")
...
>>> class User(ElasticModel):
...     id: str = Field(alias="_id")                       # "_id" maps onto "id" by default, as in pydantic
...     first_name: Annotated[str, Field(min_length=2)]
...     last_name: str
...     email: EmailStr
...     created: Created
...     updated: Created
...     def welcome(self) -> str:
...         return f"Hi {self.first_name} {self.last_name}! Joined at {self.created.datetime_from_at()}"
...
>>> # Build from a projection (partial dict): "by" is missing in both nested dicts,
>>> # and "external_value" is a key the model does not declare.
>>> doc = {
...     "_id": "u1",
...     "first_name": "Ann",
...     "last_name": "Lee",
...     "email": "ann@example.com",
...     "created": {"at": "2025-08-15"},
...     "updated": {"at": "2099-01-10"},
...     "external_value": 1,
... }
>>> # --------MAIN CONSTRUCTOR-------
>>> u = User.elastic_create(doc)
>>> # -------------------------------
>>> u.id                      # Alias works
'u1'
>>> # .elastic_extra is a plain dict holding every unknown key, kept without validation
>>> u.elastic_extra
{'external_value': 1}
>>> # The nested model is constructed, so its methods work on the data that IS loaded
>>> u.created.datetime_from_at()
datetime.datetime(2025, 8, 15, 0, 0)
>>> print(u.welcome())
Hi Ann Lee! Joined at 2025-08-15 00:00:00
>>> # Accessing a declared but not loaded field raises NotLoadedFieldError
>>> u.created.by
Traceback (most recent call last):
NotLoadedFieldError: Field 'by' is declared in Created but was not loaded into this instance
>>> # .elastic_is_loaded(name) - safe check for field presence, it never raises
>>> u.created.elastic_is_loaded("by")
False
>>> u.created.by = "system"   # Assigning to a field marks it as loaded
>>> u.created.elastic_is_loaded("by")
True
>>> # Choose the validation depth when you need it.
>>> # shallow (recursive=False) does not descend into nested models
>>> u.elastic_is_valid(recursive=False)
(True, [])
>>> # deep (recursive=True) descends and finds the field still missing in "updated"
>>> u.elastic_is_valid(recursive=True)
(False, ['updated.by'])
>>> # Produce a fully validated instance of the same model class (Self), or raise ValidationError
>>> u.updated.by = "user"     # Fill the gap first, otherwise validation raises
>>> validated = u.elastic_get_validated_model(recursive=True)
>>> type(validated) is User
True

```

---

## Comparing .elastic_create to .model_validate and .model_construct from pydantic

```python
>>> from datetime import datetime
>>> from pydantic import BaseModel, EmailStr, ValidationError
>>> from elasticmodel import ElasticModel
>>> # Three ways to build an object from the same partial data:
>>> #   1. pydantic.BaseModel.model_validate
>>> #   2. pydantic.BaseModel.model_construct
>>> #   3. elasticmodel.ElasticModel.elastic_create
>>> # Two identical model pairs, one built on BaseModel and one on ElasticModel:
>>> class CreatedPydantic(BaseModel):
...     at: str
...     by: str
...     def datetime_from_at(self) -> datetime:
...         return datetime.strptime(self.at, "%Y-%m-%d")
...
>>> class UserPydantic(BaseModel):
...     email: EmailStr
...     created: CreatedPydantic
...
>>> class CreatedElastic(ElasticModel):
...     at: str
...     by: str
...     def datetime_from_at(self) -> datetime:
...         return datetime.strptime(self.at, "%Y-%m-%d")
...
>>> class UserElastic(ElasticModel):
...     email: EmailStr
...     created: CreatedElastic
...
>>> # Equally limited data, but enough for the actions we need: "by" is missing.
>>> partial_data = {"email": "a@b.com", "created": {"at": "2025-08-15"}}
>>> # 1. pydantic.model_validate -> raises immediately
>>> UserPydantic.model_validate(partial_data)
Traceback (most recent call last):
ValidationError: 1 validation error for UserPydantic
>>> # 2. pydantic.model_construct -> does not validate, but keeps nested dicts
>>> user_construct = UserPydantic.model_construct(**partial_data)
>>> isinstance(user_construct.created, dict)   # a raw dict, so CreatedPydantic methods are not there
True
>>> user_construct.created.datetime_from_at()
Traceback (most recent call last):
AttributeError: 'dict' object has no attribute 'datetime_from_at'
>>> # 3. ElasticModel.elastic_create -> no instant failure, and nested models are built
>>> user_elastic = UserElastic.elastic_create(partial_data)
>>> isinstance(user_elastic.created, CreatedElastic)
True
>>> print(user_elastic.created.datetime_from_at())
2025-08-15 00:00:00

```

Summary:
- `model_validate`: full validation + nested building, but no partials
- `model_construct`: partials OK, but nested dicts remain dicts
- `elastic_create`: partials OK + nested building + strict read access + shallow/deep validation

---

## Key features

- Partial construction: `elastic_create(data, validate=True, strict_validate=False, defaults=False)`
  - Accepts dicts with missing and extra keys
  - Validates/coerces each value by the field rules, as `model_validate` does: the type hint, `Field(...)` constraints and the model config (`strict`, `str_strip_whitespace`, `arbitrary_types_allowed`, …)
  - Unknown keys are captured in `model.elastic_extra`
  - Tracks actually loaded fields in `model.elastic_loaded_fields`
  - `defaults=True` applies `default`/`default_factory` to missing fields; those fields are **not** counted as loaded, so `elastic_is_loaded` still reports `False` for them. A default of a field with `validate_default` is validated, as in pydantic
  - `strict_validate=True` validates in pydantic strict mode, without coercion: `"5"` is not turned into `5`. It turns validation on even with `validate=False`, nested `BaseModel` included
  - `defaults` and `strict_validate` reach nested `ElasticModel` in direct fields and inside containers. They do not reach `Optional`/`Union` fields

- Strict read access
  - Accessing an unloaded declared field raises `NotLoadedFieldError`
  - System attributes and dunders are not intercepted
  - Three limits of this guarantee, all of them by design:
    - `NotLoadedFieldError` subclasses `AttributeError`, so `hasattr(model, "field")` returns `False` and `getattr(model, "field", fallback)` returns the fallback. Both silence the violation instead of surfacing it
    - A field whose name shadows a `BaseModel` attribute (`copy`, `json`, …) is **not** covered: the normal attribute lookup finds the inherited member, so the check never runs. Pydantic warns about such shadowing when the class is declared
    - A field filled by `defaults=True` reads back its default value, even though `elastic_is_loaded` reports `False` for it

- Shallow vs Deep validation
  - Shallow: keep existing nested instances, fast
  - Deep: fully materialize to plain structures and validate everything

- Nested models and containers
  - Nested `ElasticModel` fields are built via `elastic_create`
  - `list`/`set`/`tuple` items are coerced recursively (when `validate=True`)
  - `dict[K, V]` keys and values are validated (when `validate=True`)

---

## API snapshot

- `ElasticModel.elastic_create(data: dict, validate: bool = True, strict_validate: bool = False, defaults: bool = False) -> Self`
- `model.elastic_extra -> dict[str, Any]` — a shallow copy: changing it does not change the model, but nested values are shared with it
- `model.elastic_loaded_fields -> set[str]` — a copy, so mutating it does not change the model. For a model built by pydantic validation (constructor, `model_validate`, `elastic_get_validated_model`) these are the fields present in the validation input, so a field that a default filled in the source model counts as loaded in the copy returned by `elastic_get_validated_model`
- `model.elastic_is_loaded(name: str) -> bool`
- `model.elastic_is_valid(recursive: bool = True) -> tuple[bool, list[str]]` — paths use field names, not aliases: `'created.by'`
- `model.elastic_get_validated_model(recursive: bool = True) -> Self` — the copy keeps `.elastic_extra` at every nesting level
- Assignment marks fields as loaded: `model.field = value`

---

## Defaults and config

ElasticModel recommends the following default values ​​for `pydantic.ConfigDict`:
- `extra='ignore'` — extra keys are ignored by Pydantic but manually collected into `.elastic_extra`
- `revalidate_instances='never'` — nested model instances are not revalidated automatically (important for shallow validation)

Aliases follow the same config rules as in pydantic:
- A key matching a field's alias fills that field — `validate_by_alias`, on by default.
- The field's own name is accepted for an aliased field only with `validate_by_name=True` (or the older `populate_by_name=True`).
- With `validate_by_alias=False` the alias is no longer accepted.
- If both the alias and the field name arrive, the alias wins.
- Every key that did not fill a field — unknown to the model, disallowed by the config, or beaten by the alias — lands in `.elastic_extra`. With `extra='allow'` it also lands in `model_extra`, exactly as pydantic puts it there.

A nested `BaseModel` built with `validate=False` uses the same rules. It has no `.elastic_extra`, so a key that did not fill a field goes to `model_extra` with `extra='allow'` and is dropped otherwise.

---

## Differences from BaseModel

An `ElasticModel` built the usual pydantic way (constructor, `model_validate`, `model_construct`, copies) behaves exactly like a `BaseModel`. The differences below are deliberate.

Extensions:
- `extra='forbid'` — `elastic_create` does not reject unknown keys: they land in `.elastic_extra`. Pydantic validation (constructor, `model_validate`) still rejects them.
- `==` also compares `.elastic_extra`: models with the same data but different `.elastic_extra` are not equal.
- `elastic_get_validated_model` keeps `.elastic_extra` in the copy, so a valid model equals its validated copy.

Limits of `elastic_create`:
- Class validators (`@field_validator`, `@model_validator`) do not run. They run in `elastic_get_validated_model()` and `model_validate()`. One exception comes from pydantic itself: a nested model is built first and then passed into its field as a ready instance, and pydantic runs `@model_validator(mode='wrap')` and `mode='after'` on any instance in a field. So such a validator on a nested model runs on the partial instance and must not read fields that may be unloaded.
- Partial data is not supported inside `Optional`/`Union` fields: with `validate=True` pydantic validates the value in full, so a partial dict raises `ValidationError`; with `validate=False` the dict stays a plain dict.
- On invalid data it raises `ValidationError` for the first invalid value only, and the error does not name the field.
- A `@computed_field` that reads an unloaded field raises `NotLoadedFieldError`, and so does `model_dump()`, which serializes the computed field.
- A field named `cls` is not supported: `elastic_create` builds the model with pydantic's `model_construct(cls, ...)`, which cannot take such a field either, so it raises `TypeError`. An unknown data key `cls` works as any other unknown key.

Limit of `elastic_loaded_fields`:
- With `extra='allow'`, pydantic validation puts a disallowed key that carries a field name into both `model_extra` and `model_fields_set`. So in a model built by the constructor or `model_validate`, that field counts as loaded even though it holds its default. Models built by `elastic_create` and `elastic_get_validated_model` are not affected.
