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
from types import MappingProxyType, UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    Discriminator,
    Field,
    PrivateAttr,
    PydanticUserError,
    TypeAdapter,
    ValidationError,
)
from pydantic.fields import FieldInfo

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
def _field_annotation(cls: type[BaseModel], name: str) -> Any:
    """
    Повна анотація поля `name` класу `cls` — та, з якою поле валідує `model_validate`.

    - Тип — `Annotated[анотація поля, *field.metadata]`: так діють обмеження `Field` (`gt`, `min_length`, `strict`, …).
      `discriminator` `FieldInfo` зберігає окремо від `metadata`, тому він додається власним `Field(discriminator=...)`.
      Весь `FieldInfo` передавати не можна: pydantic ≥2.12 попереджає, що `alias`, `exclude`, `validate_default` поза полем моделі не діють.
    - Анотація береться з `model_fields`: pydantic уже підставив у неї параметри generic і розв'язав анотації-рядки.

    Кеш потрібен не лише для швидкості: він віддає ту саму анотацію тим самим об'єктом.
    `FieldInfo` не має рівності за значенням, тож нова анотація на кожен виклик не влучала б у кеші, що беруть її ключем.
    """
    field = cls.model_fields[name]
    discriminator = [Field(discriminator=field.discriminator)] if field.discriminator is not None else []
    metadata = [*field.metadata, *discriminator]
    return Annotated[(field.annotation, *metadata)] if metadata else field.annotation


def _type_adapter(owner: type[BaseModel], annotation: Any) -> TypeAdapter:
    """
    `TypeAdapter` для `annotation` з конфігом моделі `owner`, тож діють `strict`, `str_strip_whitespace`, `arbitrary_types_allowed` та інші ключі конфігу.

    Тип із власним конфігом (`BaseModel`, dataclass, `TypedDict`) pydantic разом з `config` не приймає.
    Такий тип і так валідується за своїм конфігом, тому для нього адаптер будується без `config`.
    """
    try:
        return TypeAdapter(annotation, config=owner.model_config)
    except PydanticUserError as exc:
        if exc.code != 'type-adapter-config-unused':
            raise
        return TypeAdapter(annotation)


@lru_cache(maxsize=1024)
def _field_adapter(cls: type[BaseModel], name: str) -> TypeAdapter:
    """
    `TypeAdapter`, що валідує значення поля `name` так само, як його валідує `model_validate` класу `cls`:
    повна анотація поля (`_field_annotation`) з конфігом класу (`_type_adapter`).

    Кеш за парою «клас, поле», бо створення адаптера дороге, а клас з ім'ям поля однозначно задають адаптер.
    """
    return _type_adapter(cls, _field_annotation(cls, name))


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
              In a plain `Union` field the values are still checked to pick the branch, but they land in the model as is.
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
    prepared_value = _elastic_prepare_value(value, _field_annotation(cls, name), options, owner=cls)

    if options.validates:
        return _field_adapter(cls, name).validate_python(prepared_value, strict=options.strict)
    return prepared_value


def _elastic_prepare_value(value: Any, annotation: Any, options: _CreateOptions, owner: type[BaseModel]) -> Any:
    """
    Будує із сирого значення вкладені моделі, яких pydantic не збудує частковими:
    - `dict` для поля `ElasticModel` стає частковою моделлю (див. `_elastic_build_model`).
    - `dict` для поля `BaseModel` без валідації стає моделлю через `model_construct`, інакше в полі лишився б `dict`.
      З валідацією його будує й перевіряє pydantic.
    - Елементи `list`/`tuple`/`set` і значення `dict` обходяться за анотацією елемента.
    - `Optional[X]` обходиться як `X`.
    - `dict` для поля `Union` стає моделлю обраної гілки (див. `_elastic_build_union`).

    Решта значень повертається як є, і їх перевіряє pydantic.
    Зокрема значення не того типу, наприклад число для `list[int]`: pydantic поверне на нього свою помилку.

    :param annotation: анотація разом з метаданими `Annotated`: з них береться дискримінатор `Union`.
    :param owner: модель, чиє поле обходиться; з її конфігом будуються адаптери типів усередині поля.
    """
    if value is None:
        return None

    base = _strip_annot(annotation)
    origin = get_origin(base)

    if origin is Union or origin is UnionType:
        members = [member for member in get_args(base) if member is not type(None)]
        # `Optional[X]` поводиться як `X` для будь-якого значення: так обходяться й контейнери, й дискримінований `Union` усередині
        if len(members) == 1:
            return _elastic_prepare_value(value, members[0], options, owner)
        if isinstance(value, dict):
            return _elastic_build_union(value, annotation, options, owner)
        return value

    # !todo на кожному value викликається ця перевірка, можливо потрібно це робити якось оптимізованіше, перевірити що value це клас хочаб
    if isinstance(value, dict) and _issubclass_safe(base, BaseModel):
        # `BaseModel` з валідацією будує й перевіряє адаптер поля pydantic
        if options.validates and not _issubclass_safe(base, ElasticModel):
            return value
        return _elastic_build_model(base, value, options)

    if origin in (list, set, tuple) and _is_collection_input(value):
        args = get_args(base) or (Any,)
        if origin is list:
            return [_elastic_prepare_value(item, args[0], options, owner) for item in value]
        if origin is set:
            return {_elastic_prepare_value(item, args[0], options, owner) for item in value}

        # Кортеж змінної довжини: `tuple[T, ...]`
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_elastic_prepare_value(item, args[0], options, owner) for item in value)

        # Кортеж фіксованої довжини: обходяться лише описані позиції, а зайві елементи лишаються як є, щоб pydantic повідомив про довжину
        items = list(value)
        head = [_elastic_prepare_value(item, item_annotation, options, owner) for item, item_annotation in zip(items, args)]
        return tuple(head + items[len(args):])

    # Ключі словника лишаються як є: моделі можуть бути лише в значеннях
    if origin is dict and isinstance(value, Mapping):
        _, value_annotation = get_args(base) or (Any, Any)
        return {key: _elastic_prepare_value(item, value_annotation, options, owner) for key, item in value.items()}

    return value


def _elastic_build_model(model_cls: type[BaseModel], value: dict[str, Any], options: _CreateOptions) -> BaseModel:
    """
    Будує з `dict` часткову модель `model_cls`:
    - `ElasticModel` — через її `elastic_create` з тими самими `options`.
      Так параметри головного конструктора діють на всіх рівнях вкладеності, зокрема в елементах контейнерів.
    - `BaseModel` — через `model_construct` без валідації, зі значеннями полів, підготованими цим самим обходом.
      `BaseModel` з валідацією сюди не доходить: його `dict` будує й перевіряє адаптер поля pydantic.
    """
    if _issubclass_safe(model_cls, ElasticModel):
        return model_cls.elastic_create(value, validate=options.validate, strict_validate=options.strict_validate, defaults=options.defaults)

    # Ключі зіставляються з полями за тими самими правилами, що й в `elastic_create`.
    # Сховища `elastic_extra` у `BaseModel` немає, тому незаповнені ключі зберігаються лише в `model_extra` при `extra='allow'`.
    instance, _ = _construct_from_input(
        model_cls, value, lambda name, item: _elastic_prepare_value(item, _field_annotation(model_cls, name), options, owner=model_cls)
    )
    return instance


# =========================
#   Union fields of elastic_create
# =========================

def _elastic_build_union(value: dict[str, Any], annotation: Any, options: _CreateOptions, owner: type[BaseModel]) -> Any:
    """
    Будує з `dict` модель гілки поля `Union` — тієї, яку обрав би pydantic, але частковою, як пряме поле (див. `_elastic_build_model`).

    - Дискримінований `Union` (дискримінатор у метаданих `Annotated`): клас гілки береться з таблиці тегів pydantic за тегом із даних.
      Так обирає гілку сам pydantic, і код інших гілок (`model_post_init`, валідатори) не виконується.
      Немає тегу, тег невідомий або таблиці немає — значення повертається як є:
      з валідацією адаптер поля поверне на нього помилку pydantic (`union_tag_not_found`), без валідації в полі лишається `dict`.
    - Звичайний `Union` — гілка за правилом smart mode pydantic (див. `_plain_union_branch`).

    Обрана гілка-`BaseModel` з валідацією, як і пряме поле, не будується: значення лишається як є, і його будує й перевіряє адаптер поля pydantic.
    Часткових даних вона однаково не прийме, а так результат і помилки збігаються з pydantic, разом зі шляхом помилки й переходом до інших гілок.

    :param annotation: анотація `Union` разом з метаданими `Annotated`.
    """
    if any(_is_discriminator(item) for item in _annotation_metadata(annotation)):
        table = _call_with_cache(_union_tag_table, owner, annotation)
        branch = table.branch_of(value) if table is not None else None
    else:
        branch = _plain_union_branch(value, annotation, options, owner)

    if branch is None or (options.validates and not _issubclass_safe(branch, ElasticModel)):
        return value
    return _elastic_build_model(branch, value, options)


def _plain_union_branch(value: dict[str, Any], annotation: Any, options: _CreateOptions, owner: type[BaseModel]) -> type[BaseModel] | None:
    """
    Гілка-модель звичайного `Union` для значення-`dict` — за правилом, близьким до smart mode pydantic, однаково за будь-якого `validate`.

    1. Гілка-не-модель, що приймає `dict` (`dict`, `Any`, `TypedDict`), — гілку обирає pydantic: повертається `None`, і значення лишається як є.
    2. Гілки-моделі впорядковуються (див. `_ordered_model_branches`): гілка з `extra='forbid'` і чужими ключами вибуває, далі повні, далі більше заповнених полів, далі ліва.
    3. Обирається перша гілка, що приймає наявні значення (див. `_elastic_branch_accepts`); якщо гілка одна, перевірка пропускається.
       Гілка, якій дані не заповнюють жодного поля, значень не приймає.
       Якщо не приймає жодна, обирається перша: з валідацією її побудова дасть ту саму помилку, що пряме поле, без валідації вона будується зі значеннями як є.
    Якщо вибули всі гілки-моделі, повертається `None`: з валідацією pydantic поверне свою помилку, без валідації лишається `dict`.

    Розходження з pydantic, описані в README: точність збігу (`IntX` проти `StrX` для `"1"`) і валідатори класу гілки у виборі не беруть участі.

    Приклад: `Union[Card, Bank]`, дані `{"kind": "bank"}`. Обидві гілки неповні й заповнюють одне поле, тож порядок `Card`, `Bank`.
    `Card` відкидає `"bank"` для `Literal["card"]`, `Bank` приймає — обирається `Bank`.
    """
    models, others = _union_branches(_strip_annot(annotation))
    if any(_non_model_branch_accepts(owner, other, value, options) for other in others):
        return None

    candidates = _ordered_model_branches(models, value)
    if len(candidates) <= 1:
        return candidates[0] if candidates else None

    return next((branch for branch in candidates if _elastic_branch_accepts(branch, value, options)), candidates[0])


def _union_branches(union: Any) -> tuple[list[type[BaseModel]], list[Any]]:
    """
    Гілки `Union` у порядку оголошення, розділені на гілки-моделі (підкласи `BaseModel`) і решту; `None` пропускається.
    Гілки-не-моделі зберігають свої метадані `Annotated`, бо з ними pydantic їх і валідує.
    Вкладений `Union` (зокрема дискримінований як член ширшого) розгортається у свої гілки.
    """
    models: list[type[BaseModel]] = []
    others: list[Any] = []
    for member in get_args(union):
        base = _strip_annot(member)
        if base is type(None):
            continue
        if get_origin(base) is Union or get_origin(base) is UnionType:
            nested_models, nested_others = _union_branches(base)
            models += nested_models
            others += nested_others
        elif _issubclass_safe(base, BaseModel):
            models.append(base)
        else:
            others.append(member)
    return models, others


def _non_model_branch_accepts(owner: type[BaseModel], annotation: Any, value: dict[str, Any], options: _CreateOptions) -> bool:
    """Чи приймає гілка-не-модель значення: її валідує адаптер з конфігом власника поля, тож діє, зокрема, `arbitrary_types_allowed`."""
    try:
        _call_with_cache(_owner_type_adapter, owner, annotation).validate_python(value, strict=options.strict)
    except ValidationError:
        return False
    return True


@lru_cache(maxsize=256)
def _owner_type_adapter(owner: type[BaseModel], annotation: Any) -> TypeAdapter:
    """
    Кешований `_type_adapter`: адаптер типу всередині поля моделі `owner`.
    Кеш за парою «`owner`, анотація», бо `ConfigDict` не хешується, а клас однозначно задає конфіг.
    """
    return _type_adapter(owner, annotation)


def _ordered_model_branches(models: list[type[BaseModel]], value: Mapping[str, Any]) -> list[type[BaseModel]]:
    """
    Гілки-моделі в порядку вибору для значення-`dict`; екземпляри при цьому не створюються.

    - Заповнені поля гілки — ті, які заповнили б ключі значення за правилами аліасів (`_input_key_map`), тобто майбутній `model_fields_set`.
    - Гілка з `extra='forbid'` вибуває, якщо в значенні є ключ, що не заповнює її поле: pydantic таку гілку відкидає.
    - Порядок: спершу повні (заповнені всі обов'язкові поля), далі за кількістю заповнених полів за спаданням, далі за порядком оголошення.
    """
    ranked: list[tuple[bool, int, type[BaseModel]]] = []
    for model in models:
        filling_keys = _input_key_map(model).filling_keys(value)
        if model.model_config.get('extra') == 'forbid' and len(filling_keys) < len(value):
            continue

        filled = set(filling_keys.values())
        complete = all(name in filled for name, field in model.model_fields.items() if field.is_required())
        ranked.append((complete, len(filled), model))

    # Сортування стабільне, тож за рівних умов лишається порядок оголошення
    ranked.sort(key=lambda item: (not item[0], -item[1]))
    return [model for _, _, model in ranked]


def _elastic_branch_accepts(branch: type[BaseModel], value: Mapping[str, Any], options: _CreateOptions) -> bool:
    """
    Чи приймає гілка наявні значення: кожне значення, що заповнює її поле, валідується за правилами поля, як в `elastic_create` з валідацією.

    Перевірка однакова за будь-якого `validate` і з тим самим `strict_validate`, а дефолти в ній не беруть участі.
    Екземпляр гілки не створюється, тож її `model_post_init` і валідатори класу не виконуються.
    Валідатори з `Annotated` полів гілки й `wrap`/`after`-валідатори її вкладених моделей при цьому виконуються (межа описана в README).
    Повний `model_validate` тут не годиться: частковий `BaseModel` він відкидає завжди.
    """
    filling_keys = _input_key_map(branch).filling_keys(value)
    # Гілка, якій дані не заповнюють жодного поля, нічим їх не підтверджує.
    # Інакше вона «приймала» б будь-які дані, і хибне значення для іншої гілки мовчки потрапляло б у її `elastic_extra`.
    if not filling_keys:
        return False

    check = _CreateOptions(validate=True, strict_validate=options.strict_validate, defaults=False)
    try:
        for key, name in filling_keys.items():
            _elastic_coerce_value(branch, name, value[key], check)
    except ValidationError:
        return False
    return True


def _annotation_metadata(annotation: Any) -> tuple[Any, ...]:
    """Метадані `Annotated[тип, *метадані]`; для анотації без `Annotated` — порожньо."""
    return get_args(annotation)[1:] if get_origin(annotation) is Annotated else ()


def _is_discriminator(item: Any) -> bool:
    """Чи задає елемент метаданих дискримінатор `Union`: `Discriminator(...)` або `Field(discriminator=...)`."""
    return isinstance(item, Discriminator) or (isinstance(item, FieldInfo) and item.discriminator is not None)


def _call_with_cache(cached: Callable[..., Any], *args: Any) -> Any:
    """
    Викликає функцію з `lru_cache`, а для нехешованих аргументів — її саму без кешу.
    `lru_cache` на нехешованому аргументі піднімає `TypeError`; анотація буває нехешованою, якщо в її метаданих нехешований об'єкт.
    """
    try:
        hash(args)
    except TypeError:
        return cached.__wrapped__(*args)
    return cached(*args)


@dataclass(frozen=True)
class _UnionTagTable:
    """
    Таблиця вибору гілки дискримінованого `Union` — та, яку pydantic склав у схемі поля і за якою сам обирає гілку.

    - `discriminator` — callable, що повертає тег зі значення (як `Discriminator(функція)`), або шляхи ключів, за якими тег лежить у `dict`.
      Шляхів кілька, коли поле-тег приймається і за іменем, і за аліасом: `(('kind',), ('_kind',))`.
    - `choices` — тег → клас гілки. Тег — значення `Literal` поля-тегу (рядок, число, член `Enum`) або мітка `Tag`.
    """
    discriminator: Callable[[Any], Any] | tuple[tuple[str | int, ...], ...]
    choices: Mapping[Any, type[BaseModel]]

    def branch_of(self, value: Mapping[str, Any]) -> type[BaseModel] | None:
        """
        Клас гілки для значення-`dict`; `None`, якщо тегу в значенні немає або такого тегу немає в таблиці.

        Тег зіставляється лише рівністю, як у pydantic.
        Тому рядок `"card"` знаходить гілку `Literal[Kind.CARD]` лише для `str`-Enum, член якого дорівнює своєму рядку; для звичайного `Enum` pydantic дає `union_tag_invalid`.
        Перебір замість `choices.get(tag)`, бо тег з даних буває нехешованим.
        """
        tag = self.discriminator(value) if callable(self.discriminator) else _tag_by_paths(value, self.discriminator)
        if tag is None:
            return None

        for choice_tag, branch in self.choices.items():
            if choice_tag == tag:
                return branch
        return None


def _tag_by_paths(value: Mapping[str, Any], paths: tuple[tuple[str | int, ...], ...]) -> Any:
    """
    Тег за першим шляхом ключів, що повністю знаходиться в значенні; `None`, якщо не знайшовся жоден.
    Крок шляху — ключ словника або, як в `AliasPath`, індекс у списку.

    Приклад: `_tag_by_paths({"_kind": "card"}, (("kind",), ("_kind",)))` → `"card"`.
    """
    for path in paths:
        item: Any = value
        for step in path:
            if isinstance(item, Mapping) and step in item:
                item = item[step]
            elif isinstance(step, int) and isinstance(item, (list, tuple)) and -len(item) <= step < len(item):
                item = item[step]
            else:
                break
        else:
            return item
    return None


@lru_cache(maxsize=256)
def _union_tag_table(owner: type[BaseModel], annotation: Any) -> _UnionTagTable | None:
    """
    Таблиця тегів дискримінованого `Union` з анотацією `annotation` у полі моделі `owner`.

    Береться з `core_schema` адаптера `Annotated[Union, лише дискримінатори]` з конфігом `owner`: решта метаданих поля
    (обмеження, валідатори) загорнула б вузол `tagged-union` у свої вузли.
    - Перед вузлом можуть стояти `definitions` (рекурсивні моделі, callable-дискримінатор) і `nullable` (`None` серед членів).
    - Гілка — схема `model`, обгортка валідаторів класу навколо неї (`function-after` тощо) або `definition-ref` на `definitions`.
    - Гілка-не-модель (`TypedDict`, dataclass) у таблицю не потрапляє: її будує pydantic.

    `core_schema` — внутрішній формат pydantic, форма перевірена на 2.11.0, 2.11.7 і 2.14.1.
    Якщо вузла `tagged-union` немає, повертається `None`, і значення лишається як є, а не падає.

    Кеш за парою «`owner`, анотація», бо `ConfigDict` не хешується, а клас однозначно задає конфіг.
    """
    discriminators = [item for item in _annotation_metadata(annotation) if _is_discriminator(item)]
    node = _type_adapter(owner, Annotated[(_strip_annot(annotation), *discriminators)]).core_schema

    definitions: dict[str, Any] = {}
    while node['type'] in ('definitions', 'nullable'):
        if node['type'] == 'definitions':
            definitions.update((definition['ref'], definition) for definition in node['definitions'])
        node = node['schema']

    if node['type'] != 'tagged-union':
        return None

    choices = {tag: _schema_model_cls(choice, definitions) for tag, choice in node['choices'].items()}
    return _UnionTagTable(
        discriminator=_discriminator_paths(node['discriminator']),
        choices=MappingProxyType({tag: branch for tag, branch in choices.items() if branch is not None}),
    )


def _schema_model_cls(schema: Any, definitions: Mapping[str, Any]) -> type[BaseModel] | None:
    """
    Клас моделі за схемою гілки: `definition-ref` розгортається через `definitions`, обгортки валідаторів — через їхню `schema`.
    `None`, якщо під обгортками не модель.
    """
    node = schema
    while isinstance(node, Mapping):
        if node.get('type') == 'model':
            return node['cls'] if _issubclass_safe(node['cls'], BaseModel) else None
        node = definitions.get(node['schema_ref']) if node.get('type') == 'definition-ref' else node.get('schema')
    return None


def _discriminator_paths(discriminator: Any) -> Callable[[Any], Any] | tuple[tuple[str | int, ...], ...]:
    """
    Дискримінатор вузла `tagged-union` у формі `_UnionTagTable.discriminator`.
    pydantic записує його callable, рядком (`'kind'`), одним шляхом (`['kind']`) або переліком шляхів (`[['kind'], ['_kind']]`).
    """
    if callable(discriminator):
        return discriminator
    if isinstance(discriminator, str):
        return ((discriminator,),)
    if discriminator and isinstance(discriminator[0], list):
        return tuple(tuple(path) for path in discriminator)
    return (tuple(discriminator),)
