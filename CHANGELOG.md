# Changelog

## 0.3.0 — unreleased

Changes are listed per story of EM-1 (the `ElasticModel` public contract), in the order they were made.
A later story sometimes revises an earlier one; such entries say so, and the final behavior is described in the README.

### 1. `NotLoadedFieldError` for unloaded fields
- Reading a declared field that was not loaded raises `NotLoadedFieldError`, exported from `gostmodels`. It subclasses `AttributeError`, so existing `except AttributeError` code keeps working.
- The message names the field and the model.
- Undeclared names keep the standard pydantic behavior.

### 2. Safe list of loaded fields
- `elastic_loaded_fields` returns a copy, so changing it does not change the model.
- The list no longer contains the library's private attributes.

### 3. Field name accepted with both config keys
- An aliased field accepts its field name with `validate_by_name` as well as with the older `populate_by_name`.
- Revised by story 8, which applies the full pydantic key rules.

### 4. Package metadata
- `pydantic>=2.11,<3` is declared as a dependency, so a clean `pip install` gives a working package.
- Version 0.3.0.

### 5. README matches the API
- Constructor signature: `elastic_create(data, validate=True, strict_validate=False, defaults=False)`.
- Method names, `Self` return types and `elastic_loaded_fields` are documented as they are in the code.
- Fields filled by `defaults=True` are documented as not loaded.
- The read-access guarantee lists its limits: `hasattr`/`getattr` with a default, fields that shadow `BaseModel` attributes, and fields filled by `defaults=True`.

### 6. README examples are tested
- Both README examples run as doctests in the test suite, so every documented output is checked.

### 7. Release checks
- The lower bound pydantic 2.11 is confirmed: 2.10 fails, 2.11 passes.
- The package is declared explicitly for setuptools, so the build no longer picks up stray top-level folders.

### 8. Key rules as in pydantic
- Input keys fill fields by the same rules as pydantic: the alias is accepted by default, the field name only with `validate_by_name` or `populate_by_name`, `validate_by_alias=False` disables the alias, and the alias wins over the field name.
- Every key that did not fill a field lands in `elastic_extra` instead of being lost.
- A nested `BaseModel` built with `validate=False` follows the same rules.
- `elastic_is_valid` and `elastic_get_validated_model` agree for a model built by alias. Error paths use field names, not aliases.

### 9. `elastic_extra` copy and loaded fields of validated models
- `elastic_extra` returns a shallow copy: top-level keys are protected, nested values are shared with the model.
- Models built by pydantic validation (constructor, `model_validate`, `elastic_get_validated_model`) report their loaded fields. Revised by story 14, which computes them from the pydantic state.

### 10. `defaults` and `strict_validate` reach nested models
- Both parameters apply to nested `ElasticModel` in direct fields and inside containers.

### 11. Runtime annotations on Python 3.10
- `Self` is imported from `typing_extensions`, so `typing.get_type_hints` resolves the public methods.
- `typing-extensions>=4.12.2` is declared as a dependency. pydantic 2.11 requires it anyway.

### 12. README follows stories 8–11
- README describes the key rules, the `elastic_extra` copy, loaded fields of validated models and how `defaults` and `strict_validate` reach nested models.

### 13. Release checks
- The suite passes on Python 3.13 and 3.10 with pydantic 2.11. The wheel installs into a clean environment and pulls its dependencies.

### 14. The usual pydantic path behaves like `BaseModel`
- `ElasticModel` keeps no private per-instance state besides `elastic_extra`. So `==` gives the same result as on `BaseModel`, for example for a default value against the same value passed explicitly.
- Loaded fields are computed from the pydantic state on every call. They stay correct after copies, `model_construct` and `model_copy(update=...)`.
- `elastic_create` works on models with `serialize_by_alias=True` and on `frozen=True` models.
- `==` also compares `elastic_extra`: models with the same data but different unknown keys are not equal.

### 15. Validation from the current model state
- `elastic_is_valid` and `elastic_get_validated_model` no longer serialize the model. `serialize_by_alias`, `exclude`, `@field_serializer` and `@computed_field` no longer make a valid model look invalid.
- A `model_extra` key that carries a field name no longer fills that field during validation.
- The validated copy keeps `elastic_extra` at every nesting level, so a valid model equals its validated copy.

### 16. `model_extra` as in pydantic with `extra='allow'`
- A key that did not fill a field also lands in `model_extra`, exactly as pydantic puts it there.
- With `extra='forbid'`, `elastic_create` puts unknown keys into `elastic_extra` without an error. This is a deliberate extension.

### 17. Values validated by the field rules
- `elastic_create(validate=True)` validates each value as `model_validate` does: `Field(...)` constraints, the model config (`strict`, `str_strip_whitespace`, `arbitrary_types_allowed`, …), generic parameters, and `validate_default` with `defaults=True`.
- Models declared inside a function that refer to each other by a string annotation now work.

### 18. Fields with `validation_alias`
- A `validation_alias` key no longer fills the field without validation: it lands in `elastic_extra`.
- For a field with its own `validation_alias`, the alias and the field name are accepted by the pydantic rules.
- Reading values by `validation_alias` is still not supported.

### 19. README: differences from `BaseModel`
- New section "Differences from BaseModel" lists the deliberate extensions and the limits of `elastic_create`.
- Models with `@computed_field` are supported by `elastic_create`. A computed field that reads an unloaded field raises `NotLoadedFieldError`, and so does `model_dump()`.

### 20. Supported versions and release checks
- Python 3.11+ is supported. Python 3.10 is not blocked but not supported: there, `elastic_create` fails on a string forward reference inside a builtin generic such as `children: list["Node"]`. On 3.10 use `List["Node"]` or `from __future__ import annotations`.
- The suite passes on Python 3.13 with pydantic 2.11.7 and 2.14.1, and on Python 3.10, where the two tests of the known issue are skipped.

### 21. Review of pydantic mechanisms
- The code was reviewed for parts that pydantic could do itself. No public pydantic mechanism builds partial models without changing the usual `BaseModel` path, so the library keeps its own construction code. Dead code was removed.
- README now states that pydantic runs `@model_validator(mode='wrap')` and `mode='after'` of a nested model on the partial instance, as it does for any instance passed into a field.

### 22. Invalid container values and code simplification
- A container field with a value of another type fails as in pydantic. Before, `elastic_create({"x": 5})` for `x: list[int]` raised `TypeError`, and a string for `list[int]` was split into characters, so the error was `int_parsing` instead of `list_type`. The same applied to `tuple`, `set` and `dict` fields.
- `strict_validate=True` with `validate=False` now validates a nested `BaseModel` too, as it does for a direct field. The limit is removed from README.
- Internal: one construction path for `elastic_create` and for a nested `BaseModel` built without validation, the constructor parameters in one object, unused code removed.
