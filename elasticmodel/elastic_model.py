"""
ElasticModel is a base class for working with partial (projection) documents from a database.

It allows you to create objects from truncated dicts, ignoring missing required fields, but:
- will throw an error when accessing an unloaded field (NotLoadedFieldError);
- validates loaded values by the field rules, as `model_validate` does (email, datetime, enum, `Field` constraints, …);
- recursively builds nested models (which also inherit ElasticModel);
- puts extra keys in .elastic_extra (without validation);
- supports two validation modes: deep and shallow.

ElasticModel — базовий клас для роботи з частковими (проекційними) документами з бази даних.

Дозволяє створювати об'єкти з урізаних dict'ів, ігноруючи відсутні обов'язкові поля, але:
- кидає помилку при доступі до незавантаженого поля (NotLoadedFieldError);
- валідує завантажені значення за правилами поля, як `model_validate` (email, datetime, enum, обмеження `Field`, …);
- рекурсивно будує вкладені моделі (які також наслідують ElasticModel);
- поміщає зайві ключі в .elastic_extra (без валідації);
- підтримує два режими валідації: глибокий та поверхневий.
"""

# pydantic опис методів:
# .model_extra - Невідомі моделі поля (зберігаються в окремому місці в залежності від `ConfigDict.extra`), до цих полів можна отримати доступ як і до всіх інших полів через instance.key
# .__dict__ - Відомі моделі заповнені поля (Юзерські + Дефолтні)
# .model_dump() - `.__dict__` + `.model_extra`. Відображає всі поля які доступні через "."
# .model_fields_set / __pydantic_fields_set__ - Відомі моделі поля які встановлені виключно юзером (без дефолтів, та без `.model_extra`). 
#   (Увага!. Масив не реагує на видалення полів (якщо юзер видалить поле, воно все ще залишиться тут), зате реагує на додавання полів)
# 

# elastic_model опис методів:
# .elastic_loaded_fields - Відомі та не відомі моделі юзерські поля (без дефолтів, + `.model_extra`, які прямо доступні через "."
#   - Завжди показує актуальне значення завантажених полів, (По суті комбінація `model_extra + .model_fields_set`, але навідміну від `.model_fields_set` точніше за рахунок видалення (актуальності) полів) 
#   - Якщо юзер видалить завантажене поле - воно також зникне з `.elastic_loaded_fields` (навідміну від `model_fields_set`), 

# .elastic_extra - невідомі моделі поля, завжди зберігаються незважаючи на `ConfigDict.extra`,
#   - Дозволяє зберігати ці поля навіть при `ConfigDict.extra='ignore'`,
#     (`ConfigDict.extra='ignore'` дозволяє не засмічувати модель лишніми полями, як при `ConfigDict.extra='allow'`)


from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    TypeVar,
    get_args,
    get_origin,
)

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    Field,
    PrivateAttr,
    PydanticUserError,
    TypeAdapter,
    ValidationError,
)

# `Self` береться з `typing_extensions`, бо в `typing` він з'явився лише в 3.11, а пакет підтримує 3.10.
# Імпорт рантаймовий, а не під `TYPE_CHECKING`: інакше `typing.get_type_hints` не розв'язує анотації повернення публічних методів.
from typing_extensions import Self

_ModelT = TypeVar('_ModelT', bound=BaseModel)

# Імена параметрів `model_construct(cls, _fields_set=None, **values)`.
# Ключ даних з таким ім'ям, переданий у `**values`, `model_construct` прийняв би як свій параметр:
# `cls` дає `TypeError`, а `_fields_set` підміняє перелік заповнених полів, і поля з даних стають незавантаженими.
# Тому такі ключі `model_construct` не отримує; в `elastic_extra` і `model_extra` вони потрапляють, як будь-який невідомий ключ.
_MODEL_CONSTRUCT_PARAMS = frozenset({'cls', '_fields_set'})

# =========================
#   Helper Entities
# =========================

class NotLoadedFieldError(AttributeError):
    """
    The field is declared in the model but was not loaded into this instance, so there is nothing to read.

    Inherits `AttributeError` on purpose: existing code that catches the standard attribute error keeps working.
    The flip side is that `hasattr` returns False and `getattr` with a default returns that default — both silence the contract violation.

    Поле оголошене в моделі, але не було завантажене в цей інстанс, тому читати його нічим.

    Нащадок `AttributeError` навмисно: наявний код, який ловить штатну помилку доступу, лишається робочим.
    Зворотний бік — `hasattr` дає False, а `getattr` з дефолтом віддає дефолт, тобто обидва глушать порушення контракту.
    """


def _issubclass_safe(tp: Any, base: type) -> bool:
    """
    Safe issubclass check for cases where tp might not be a class (e.g., typing constructs).
    Used only as a safe check. The function guarantees False instead of an error.
    
    Безпечна перевірка issubclass для випадків, коли tp може бути не-класом (наприклад, typing-конструкції).
    Використовується лише як безпечна перевірка. Функція гарантує False замість помилки.
    """
    try:
        return isinstance(tp, type) and issubclass(tp, base)
    except TypeError:
        return False


@dataclass(frozen=True)
class _InputKeyMap:
    """
    Відповідність вхідних ключів полям моделі — за тими самими правилами конфігу, за якими дані приймає pydantic.

    Потрібна там, де модель будується без валідації pydantic (`model_construct`), тобто правила аліасів доводиться застосовувати самим.
    Будується раз на клас через `_input_key_map`, тому екземпляр спільний і незмінний.

    - `accepted` — вхідний ключ → ім'я поля, лише для ключів, які конфіг дозволяє.
    - `aliases` — ключі з `accepted`, що є аліасами; потрібні, щоб аліас вигравав у імені того самого поля.
    - `field_keys` — усі імена, аліаси й ключі `validation_alias` полів, дозволені чи ні.
      Ключ звідси, якого немає в `accepted`, заборонений конфігом, і передавати його в `model_construct` не можна: той приймає ім'я, аліас і `validation_alias` безумовно, тож заповнив би поле в обхід конфігу й без валідації.
    """
    accepted: Mapping[str, str]
    aliases: frozenset[str]
    field_keys: frozenset[str]

    def filling_keys(self, data_keys: Iterable[str]) -> dict[str, str]:
        """
        Обирає серед вхідних ключів ті, що заповнять поля, і повертає `{вхідний ключ: ім'я поля}`.

        Якщо для поля прийшли і аліас, і ім'я, виграє аліас незалежно від порядку ключів — так робить pydantic.
        Ключ, якого немає в результаті, поле не заповнює: він невідомий, заборонений конфігом або програв аліасу.

        Приклад (дозволені і аліас `_id`, і ім'я `id`): `filling_keys(["id", "_id", "x"])` → `{"_id": "id"}`.
        """
        winner_by_field: dict[str, str] = {}
        for key in data_keys:
            field_name = self.accepted.get(key)
            if field_name is None:
                continue
            if field_name not in winner_by_field or key in self.aliases:
                winner_by_field[field_name] = key

        return {key: field_name for field_name, key in winner_by_field.items()}


@lru_cache(maxsize=256)
def _input_key_map(cls: type[BaseModel]) -> _InputKeyMap:
    """
    Будує таблицю вхідних ключів для класу моделі за правилами pydantic:
    - поле без аліасу приймається за іменем завжди;
    - аліас поля приймається, якщо дозволяє `validate_by_alias` (типово так);
    - ім'я аліасованого поля приймається, якщо дозволяє `validate_by_name` або старий `populate_by_name` (типово ні).
    - поле з власним `validation_alias` (заданим і відмінним від `alias`) pydantic валідує лише за `validation_alias` і, якщо дозволено, за іменем.
      Тому `alias` такого поля не приймається, ім'я — лише з дозволом, а ключі `validation_alias` не приймаються, бо їх читання поза межами бібліотеки.
      Звичайне аліасоване поле сюди не потрапляє: pydantic копіює `alias` у `validation_alias`, коли той не заданий.

    Обидва ключі «ім'я дозволене» читаються незалежно, бо pydantic не розгортає актуальний ключ у старий.
    """
    config = cls.model_config
    by_alias = config.get('validate_by_alias', True)
    by_name = config.get('validate_by_name', False) or config.get('populate_by_name', False)

    accepted: dict[str, str] = {}
    aliases: set[str] = set()
    field_keys: set[str] = set()

    for name, field in cls.model_fields.items():
        field_keys.add(name)
        if field.alias is not None:
            field_keys.add(field.alias)

        if field.validation_alias is not None and field.validation_alias != field.alias:
            field_keys.update(_validation_alias_keys(field.validation_alias))
            if by_name:
                accepted[name] = name
            continue

        if field.alias is None:
            accepted[name] = name
            continue

        if by_alias:
            accepted[field.alias] = name
            aliases.add(field.alias)
        if by_name:
            accepted[name] = name

    return _InputKeyMap(accepted=MappingProxyType(accepted), aliases=frozenset(aliases), field_keys=frozenset(field_keys))


def _validation_alias_keys(validation_alias: str | AliasPath | AliasChoices) -> set[str]:
    """
    Вхідні ключі верхнього рівня, з яких pydantic читає поле за `validation_alias`: рядок, перший елемент `AliasPath`, кожен варіант `AliasChoices`.

    Приклад: `AliasChoices('_x', AliasPath('meta', 0))` → `{'_x', 'meta'}`.
    """
    choices = validation_alias.choices if isinstance(validation_alias, AliasChoices) else [validation_alias]
    # Перший елемент `AliasPath` — завжди ключ-рядок (так вимагає конструктор), але тип `path` — `list[str | int]`
    return {str(choice.path[0]) if isinstance(choice, AliasPath) else choice for choice in choices}


@dataclass(frozen=True)
class _CreateOptions:
    """
    Параметри `elastic_create`, що діють на всіх рівнях вкладеності: їх отримують вкладені моделі й елементи контейнерів.
    """
    validate: bool
    strict_validate: bool
    defaults: bool

    @property
    def validates(self) -> bool:
        """Чи валідуються значення: `strict_validate` вмикає валідацію й при `validate=False`."""
        return self.validate or self.strict_validate

    @property
    def strict(self) -> bool | None:
        """
        `strict` для валідації pydantic.
        `None` означає «як у конфігу»: явне `False` скасувало б `strict=True` моделі.
        """
        return True if self.strict_validate else None


def _construct_from_input(model_cls: type[_ModelT], data: Mapping[str, Any], convert: Callable[[str, Any], Any]) -> tuple[_ModelT, dict[str, Any]]:
    """
    Збирає модель через `model_construct` за правилами вхідних ключів pydantic і повертає її разом із ключами, що не заповнили поле.

    - Ключ, що заповнює поле, дає йому значення `convert(ім'я поля, значення)`.
    - Невідомий ключ віддається `model_construct`, і той вирішує його долю за `ConfigDict.extra`.
      Виняток — ключі з іменами параметрів самого `model_construct` (`_MODEL_CONSTRUCT_PARAMS`).
    - Ключ з ім'ям чи аліасом поля, що поле не заповнив (заборонений конфігом або програв аліасу), `model_construct` не віддається:
      той приймає такі ключі безумовно й заповнив би ним поле в обхід конфігу.
    - При `extra='allow'` `model_extra` отримує кожен ключ, що не заповнив поле, у порядку вхідних даних — так робить pydantic.
      При інших значеннях `extra` pydantic такі ключі відкидає, і `model_extra` лишається `None`.

    `model_construct` кладе в `model_fields_set` рівно заповнені поля, тому облік завантажених полів далі веде сам pydantic.

    Приклад (`validate_by_name=True`, `extra='allow'`): дані `{"_id": "A", "id": "N", "x": 1}` → поле `id == "A"`,
    `model_extra == {"id": "N", "x": 1}`, і ті самі два ключі повертаються як незаповнені.
    """
    key_map = _input_key_map(model_cls)
    filling_keys = key_map.filling_keys(data)

    construct_data: dict[str, Any] = {}
    unfilled: dict[str, Any] = {}
    for key, value in data.items():
        field_name = filling_keys.get(key)
        if field_name is not None:
            construct_data[field_name] = convert(field_name, value)
            continue

        unfilled[key] = value
        if key not in key_map.field_keys and key not in _MODEL_CONSTRUCT_PARAMS:
            construct_data[key] = value

    instance = model_cls.model_construct(**construct_data)

    # Встановлюємо так само, як `model_construct`: в обхід `__setattr__`, тож frozen-модель не заважає
    if instance.__pydantic_extra__ is not None:
        object.__setattr__(instance, '__pydantic_extra__', dict(unfilled))
    return instance, unfilled


@lru_cache(maxsize=1024)
def _field_adapter(cls: type[BaseModel], name: str) -> TypeAdapter:
    """
    `TypeAdapter`, що валідує значення поля `name` так само, як його валідує `model_validate` класу `cls`.

    - Тип — `Annotated[анотація поля, *field.metadata]`: так діють обмеження `Field` (`gt`, `min_length`, `strict`, …).
      `discriminator` `FieldInfo` зберігає окремо від `metadata`, тому він додається власним `Field(discriminator=...)`.
      Весь `FieldInfo` передавати не можна: pydantic ≥2.12 попереджає, що `alias`, `exclude`, `validate_default` поза полем моделі не діють.
    - Анотація береться з `model_fields`: pydantic уже підставив у неї параметри generic і розв'язав анотації-рядки.
    - Конфіг — `cls.model_config`, тож діють `strict`, `str_strip_whitespace`, `arbitrary_types_allowed` та інші ключі конфігу.
      Тип із власним конфігом (`BaseModel`, dataclass, `TypedDict`) pydantic разом з `config` не приймає.
      Такий тип і так валідується за своїм конфігом, тому для нього адаптер будується без `config`.

    Кеш за парою «клас, поле», бо `FieldInfo` не хешується, а клас з ім'ям поля однозначно задають адаптер.
    """
    field = cls.model_fields[name]
    discriminator = [Field(discriminator=field.discriminator)] if field.discriminator is not None else []
    metadata = [*field.metadata, *discriminator]
    annotation = Annotated[(field.annotation, *metadata)] if metadata else field.annotation

    try:
        return TypeAdapter(annotation, config=cls.model_config)
    except PydanticUserError as exc:
        if exc.code != 'type-adapter-config-unused':
            raise
        return TypeAdapter(annotation)


def _validate_constructed_defaults(instance: BaseModel, strict: bool | None) -> None:
    """
    Валідує дефолти, які `model_construct` підставив без валідації, для полів з увімкненим `validate_default`.

    Так робить `model_validate`: дефолт поля валідується, якщо `validate_default` увімкнений у полі, а якщо поле його не задає — у конфігу моделі.
    Дефолт — це поле поза `model_fields_set`, яке все ж є в `__dict__`.
    Значення записується в `__dict__` напряму, бо присвоєння frozen-модель забороняє.
    """
    cls = type(instance)
    config_validate_default = cls.model_config.get('validate_default', False)

    for name, field in cls.model_fields.items():
        if name in instance.model_fields_set or name not in instance.__dict__:
            continue

        validate_default = config_validate_default if field.validate_default is None else field.validate_default
        if validate_default:
            vars(instance)[name] = _field_adapter(cls, name).validate_python(instance.__dict__[name], strict=strict)


def _strip_annot(annotation: Any) -> Any:
    """
    Removes only the outer wrapper Annotated[..., meta], returning the base type for structure analysis (Union/list/dict/tuple/class).
    IMPORTANT: for actual validation, use the ORIGINAL annotation (with metadata).
    
    Знімає лише зовнішню обгортку Annotated[..., meta], повертаючи базовий тип для аналізу структури (Union/list/dict/tuple/клас).
    ВАЖЛИВО: для самої валідації використовуйте ОРИГІНАЛЬНУ анотацію (з метаданими).
    """

    origin = get_origin(annotation)
    if origin is Annotated:
        return get_args(annotation)[0]
    else:
        return annotation


def _is_collection_input(value: Any) -> bool:
    """
    Чи приймає pydantic значення як вхід для поля `list`/`tuple`/`set`: будь-який ітерабельний об'єкт, крім рядка, байтів і словника.

    Приклад: `[1]`, `(1,)`, `{1}`, генератор → True; `5`, `"abc"`, `{"a": 1}` → False.
    """
    return isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray, Mapping))


def _build_validation_payload(model: BaseModel, recursive: bool) -> dict[str, Any]:
    """
    Payload для повної перевірки моделі через `model_validate(..., by_alias=False, by_name=True)` — поточний стан моделі, а не вивід серіалізатора.

    Серіалізатор не годиться: він застосовує налаштування виводу (`serialize_by_alias`, `exclude`, `@field_serializer`, `@computed_field`),
    і валідна модель отримала б хибне «невалідно».

    - Поля беруться з `__dict__`, разом із дефолтами.
    - Ключі `model_extra` додаються, крім тих, що збігаються з іменем поля: при `by_name=True` такий ключ заповнив би поле чужим значенням.
      Ключ з іменем аліасу додається, бо валідація йде з `by_alias=False` і поля він не заповнить.

    Parameters:
    - recursive:
        - True — вкладені моделі теж розгортаються в payload, тож pydantic перевіряє всі рівні наново.
        - False — вкладені моделі лишаються екземплярами, і pydantic приймає їх як є.
          Якщо `ConfigDict.revalidate_instances != 'never'`, pydantic перевалідує й їх.
    """
    model_fields = type(model).model_fields
    payload = dict(model.__dict__)
    payload.update({key: value for key, value in (model.model_extra or {}).items() if key not in model_fields})

    if recursive:
        payload = {key: _build_validation_value(value) for key, value in payload.items()}
    return payload


def _build_validation_value(value: Any) -> Any:
    """
    Розгортає вкладені моделі в payload глибокої перевірки, зберігаючи форму контейнерів (`list`, `tuple`, `dict`).

    Значення в `set`/`frozenset` лишаються як є: модель у множині мусить бути хешованою, а її payload-словник — ні.
    """
    if isinstance(value, BaseModel):
        return _build_validation_payload(value, recursive=True)
    if isinstance(value, list):
        return [_build_validation_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_build_validation_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _build_validation_value(item) for key, item in value.items()}
    return value


def _carry_state_to_validated(source: Any, validated: Any) -> None:
    """
    Переносить у щойно провалідовану копію те, чого payload перевірки нести не може, на всіх рівнях вкладеності:
    - `elastic_extra` — це не дані моделі, тому в payload його немає;
    - ключі `model_extra` з іменем поля — у payload такий ключ заповнив би поле.

    Джерело й копія обходяться паралельно: моделі одного класу — за спільними полями, `list`/`tuple` однакової довжини — попарно, `dict` — за спільними ключами.
    Значення різних класів пропускаються: `Union` міг обрати іншу гілку, і переносити стан нікуди.
    Значення в `set` не обходяться, бо пар у множині немає.

    Приклад: при глибокій перевірці `validated.created` будується з payload наново, тому його `elastic_extra` порожній.
    Обхід копіює туди `elastic_extra` з `source.created`.
    """
    # Той самий екземпляр (shallow-перевірка передала вкладену модель як є) — переносити нічого
    if validated is source:
        return

    if isinstance(source, BaseModel):
        if type(validated) is not type(source):
            return

        if isinstance(source, ElasticModel) and isinstance(validated, ElasticModel):
            validated._elastic_extra = dict(source._elastic_extra)

        if validated.__pydantic_extra__ is not None:
            model_fields = type(source).model_fields
            for key, value in (source.model_extra or {}).items():
                if key in model_fields:
                    validated.__pydantic_extra__[key] = value

        for name, value in source.__dict__.items():
            if name in validated.__dict__:
                _carry_state_to_validated(value, validated.__dict__[name])

    elif isinstance(source, (list, tuple)) and isinstance(validated, (list, tuple)) and len(source) == len(validated):
        for source_item, validated_item in zip(source, validated):
            _carry_state_to_validated(source_item, validated_item)

    elif isinstance(source, dict) and isinstance(validated, dict):
        for key, value in source.items():
            if key in validated:
                _carry_state_to_validated(value, validated[key])


def _format_error_loc(loc: tuple[int | str, ...]) -> str:
    """
    Шлях помилки pydantic одним рядком: імена через крапку, індекси в квадратних дужках.

    Приклад: `('a', 'b', 2, 'c')` → `'a.b[2].c'`; `(0, 'x')` → `'[0].x'`.
    """
    parts: list[str] = []
    for part in loc:
        if isinstance(part, int) and parts:
            parts[-1] += f'[{part}]'
        else:
            parts.append(f'[{part}]' if isinstance(part, int) else str(part))
    return '.'.join(parts)


# =========================
#   Main Class 
# =========================


class ElasticModel(BaseModel):
    """
    A wrapper class around 'BaseModel' that allows constructing model objects with various data sets:
        - Without requiring all fields
        - With extra fields important for context, which will be saved in `.elastic_extra`, for access to them

    This will allow us to create models with limited/excessive data obtained from external resources (e.g., DB)

    Mechanics:
    - Create instances from projection documents via elastic_create().
    - Access to unloaded fields throws NotLoadedFieldError.
    - Extra keys available in .elastic_extra (without validation).
    - Nested ElasticModels work recursively with the same semantics.
    - For "full" validation and running class validators, use elastic_get_validated_model().
    """

    # Єдиний власний стан моделі: ключі з даних `elastic_create`, що не заповнили поле (без валідації).
    # Облік завантажених полів не зберігається, а обчислюється зі стану pydantic (див. `elastic_loaded_fields`).
    # Причина: pydantic порівнює приватні атрибути в `==`, тож стан, що різниться для однакових даних, зламав би рівність моделей.
    _elastic_extra: dict[str, Any] = PrivateAttr(default_factory=dict)


    @classmethod
    def elastic_create(
        cls,
        data: dict[str, Any],
        validate: bool = True,
        strict_validate: bool = False,
        defaults: bool = False
    ) -> Self:
        """
        Build a partial class instance from a truncated dict.

        :param data: `dict` (may contain partial fields and "extra" keys which will go to `.elastic_extra`).
        :param validate:
            - If True (default) - all values from `data` will be validated and converted by the field rules, as `model_validate` does: annotation, `Field` constraints and the model config.
            - If False - values are accepted "as is" (only for trusted flows).
        :param strict_validate:
            - If True - values are validated in pydantic strict mode, without coercion: `"5"` is not turned into `5`.
              Turns validation on even with `validate=False`.
        :param defaults:
            - If True - `default`/`default_factory` are substituted for missing fields.
              When values are validated, a default of a field with `validate_default` is validated too, as in `model_validate`.
            - If False - missing fields are not created; accessing them will throw `NotLoadedFieldError`.

        :return Class instance with:
            - Only those fields set that are in `data` (And default values if `defaults == True`)

            - `.elastic_extra` - dictionary with all unknown keys at model level (without validation),
            (If more fields are passed than described in the model, they will be in this dictionary);

            - `.elastic_loaded_fields` - set of fields that were actually set.

        Note:
        - Class validators (`field_validator`/`model_validator`) are NOT run at this stage. Run them via `elastic_get_validated_model()` or regular `model_validate()`.
        """
        options = _CreateOptions(validate=validate, strict_validate=strict_validate, defaults=defaults)

        # Ключі розкладаються за правилами аліасів pydantic (див. `_InputKeyMap`), значення полів готуються за `options`
        instance, unfilled = _construct_from_input(cls, data, lambda name, value: _elastic_coerce_value(cls, name, value, options))

        # Незавантажені дефолти — це поля, яких немає в `model_fields_set`: їх заповнив `model_construct`, а не дані.
        # Із дефолтами вони валідуються за `validate_default`, як у `model_validate`, а без дефолтів прибираються з `__dict__` напряму, бо `delattr` frozen-модель забороняє.
        if defaults:
            if options.validates:
                _validate_constructed_defaults(instance, strict=options.strict)
        else:
            for name in cls.model_fields:
                if name not in instance.model_fields_set:
                    vars(instance).pop(name, None)

        instance._elastic_extra = unfilled
        return instance

    @property
    def elastic_extra(self) -> dict[str, Any]:
        """
        Ключі з даних, які не заповнили поле моделі, без валідації: невідомі моделі, заборонені конфігом аліасів або ті, що програли аліасу.

        Віддається поверхнева копія: зміна словника не змінює модель, але вкладені значення (списки, словники) спільні з моделлю.
        """
        return dict(self._elastic_extra)

    @property
    def elastic_loaded_fields(self) -> set[str]:
        """
        Поля, які справді завантажені в цей інстанс: прийшли з даних (`elastic_create` чи валідація pydantic) або присвоєні пізніше.
        Дефолти не рахуються; невідомі ключі з даних при `extra='allow'` рахуються.

        Набір обчислюється зі стану pydantic на кожне читання, тому завжди відповідає моделі, зокрема після копій і `del`.
        Його зміна на модель не впливає.
        """
        model_fields = type(self).model_fields
        fields = {name for name in self.model_fields_set if self.elastic_is_loaded(name)}
        extra = {key for key in (self.model_extra or {}) if key not in model_fields}
        return fields | extra

    def elastic_is_loaded(self, name: str) -> bool:
        """
        Чи завантажене поле в цей інстанс: прийшло з даних (`elastic_create` чи валідація pydantic) або присвоєне пізніше.

        Оголошене поле завантажене, якщо воно в `model_fields_set` і досі присутнє в інстансі:
        - `model_fields_set` відрізняє дані від дефолту: pydantic кладе туди поля з вхідних даних і з присвоєнь, а дефолти — ні.
        - Присутність в `__dict__` потрібна, бо `model_fields_set` не реагує на `del`.

        Невідомий ключ завантажений, якщо він є в `model_extra`.
        """
        if name in type(self).model_fields:
            return name in self.model_fields_set and name in self.__dict__
        return name in (self.model_extra or {})
    
    def elastic_is_valid(self, recursive: bool = True) -> tuple[bool, list[str]]:
        """
        Checks validity without returning a new instance.
        Returns (True/False, bad_paths: List[str]), where bad_paths — 'a.b[2].c' etc.
        """
        try:
            self._elastic_validate(recursive=recursive)
            return True, []
        except ValidationError as e:
            return False, [_format_error_loc(error["loc"]) for error in e.errors()]

    def elastic_get_validated_model(self, recursive: bool = True) -> Self:
        """
        Full validation of current model state:
        - If validation is successful - returns a new class instance
        - If validation is unsuccessful - throws ValidationError.

        (If you just want to know if this object is valid — use `elastic_is_valid()`)

        Копія отримує `elastic_extra` джерела на всіх рівнях вкладеності (див. `_carry_state_to_validated`).
        Тому валідна модель без коерсії значень дорівнює своїй копії.
        """
        validated = self._elastic_validate(recursive=recursive)
        _carry_state_to_validated(source=self, validated=validated)
        return validated

    def _elastic_validate(self, recursive: bool) -> Self:
        """
        Повна валідація поточного стану моделі, спільна для `elastic_is_valid` і `elastic_get_validated_model`.

        Payload складається з імен полів, бо аліаси вже зіставлено в `elastic_create`.
        Тому валідація дозволяє ім'я поля й вимикає аліаси незалежно від конфігу моделі.
        Один метод на обидва місця тримає їхні параметри однаковими: раніше вони розходилися, і `elastic_is_valid` відкидав модель, яку `elastic_get_validated_model` приймав.
        """
        payload = _build_validation_payload(model=self, recursive=recursive)
        return self.__class__.model_validate(payload, by_alias=False, by_name=True)

    # ---------------------------
    # Access/Assignment Behavior
    # ---------------------------

    # Блок прихований від тайпчекерів так само, як `BaseModel.__getattr__` у pydantic.
    # Видимий `__getattr__` змусив би тайпчекер вважати допустимим звертання до будь-якого атрибута моделі, тож він перестав би ловити опечатки.
    if not TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any:
            """
            Перехоплює читання лише тих імен, яких не знайшов звичайний пошук атрибута.

            Завантажене поле сюди не доходить, тому на успішному читанні не виконується жодного коду.
            Оголошене, але не завантажене поле дає `NotLoadedFieldError`.
            Решта імен делегується в pydantic: він віддасть значення при `extra='allow'` або підніме штатну помилку доступу.
            """
            # Перелік полів беремо з класу: звернення через інстанс знову входить у пошук атрибута, тобто ризик рекурсії всередині перехоплювача.
            if name in type(self).model_fields:
                raise NotLoadedFieldError(f"Field '{name}' is declared in {type(self).__name__} but was not loaded into this instance")

            return super().__getattr__(name)


# =========================
#   Values of elastic_create
# =========================

def _elastic_coerce_value(cls: type[BaseModel], name: str, value: Any, options: _CreateOptions) -> Any:
    """
    Готує для `elastic_create` значення поля `name` класу `cls`:
    - з валідацією (`options.validates`) — валідує й приводить його за правилами поля pydantic (див. `_field_adapter`);
    - без валідації — лише будує вкладені `ElasticModel` і `BaseModel` (див. `_elastic_prepare_value`).
    """
    prepared_value = _elastic_prepare_value(value, cls.model_fields[name].annotation, options)

    if options.validates:
        return _field_adapter(cls, name).validate_python(prepared_value, strict=options.strict)
    return prepared_value


def _elastic_prepare_value(value: Any, annotation: Any, options: _CreateOptions) -> Any:
    """
    Будує із сирого значення вкладені моделі, яких pydantic не збудує частковими:
    - `dict` для поля `ElasticModel` стає частковою моделлю через її `elastic_create` з тими самими `options`.
      Так параметри головного конструктора діють на всіх рівнях вкладеності, зокрема в елементах контейнерів.
    - `dict` для поля `BaseModel` без валідації стає моделлю через `model_construct`, інакше в полі лишився б `dict`.
      З валідацією його будує й перевіряє pydantic.
    - Елементи `list`/`tuple`/`set` і значення `dict` обходяться за анотацією елемента.

    Решта значень повертається як є, і їх перевіряє pydantic.
    Зокрема значення не того типу, наприклад число для `list[int]`: pydantic поверне на нього свою помилку.
    `Optional` і `Union` не обходяться, бо гілку обирає pydantic; тому без валідації модель у такому полі лишається `dict` (обмеження описане в README).
    """
    if value is None:
        return None

    base = _strip_annot(annotation)
    origin = get_origin(base)

    # !todo на кожному value викликається ця перевірка, можливо потрібно це робити якось оптимізованіше, перевірити що value це клас хочаб
    if isinstance(value, dict):
        if _issubclass_safe(base, ElasticModel):
            return base.elastic_create(value, validate=options.validate, strict_validate=options.strict_validate, defaults=options.defaults)

        if _issubclass_safe(base, BaseModel):
            if options.validates:
                return value

            # Ключі зіставляються з полями за тими самими правилами, що й в `elastic_create`.
            # Сховища `elastic_extra` у `BaseModel` немає, тому незаповнені ключі зберігаються лише в `model_extra` при `extra='allow'`.
            fields = base.model_fields
            instance, _ = _construct_from_input(base, value, lambda name, item: _elastic_prepare_value(item, fields[name].annotation, options))
            return instance

    if origin in (list, set, tuple) and _is_collection_input(value):
        args = get_args(base) or (Any,)
        if origin is list:
            return [_elastic_prepare_value(item, args[0], options) for item in value]
        if origin is set:
            return {_elastic_prepare_value(item, args[0], options) for item in value}

        # Кортеж змінної довжини: `tuple[T, ...]`
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_elastic_prepare_value(item, args[0], options) for item in value)

        # Кортеж фіксованої довжини: обходяться лише описані позиції, а зайві елементи лишаються як є, щоб pydantic повідомив про довжину
        items = list(value)
        head = [_elastic_prepare_value(item, item_annotation, options) for item, item_annotation in zip(items, args)]
        return tuple(head + items[len(args):])

    # Ключі словника лишаються як є: моделі можуть бути лише в значеннях
    if origin is dict and isinstance(value, Mapping):
        _, value_annotation = get_args(base) or (Any, Any)
        return {key: _elastic_prepare_value(item, value_annotation, options) for key, item in value.items()}

    return value
