# -*- coding: utf-8 -*-
# Cover / Покривають:
# - partial-creation from projections / partial-створення з проєкцій
# - strict access to unloaded fields (AttributeError) / строгий доступ до незавантажених полів (AttributeError)
# - recursion for nested models / рекурсію для вкладених моделей
# - extra-fields / extra-поля
# - alias
# - shallow vs deep validation (is_valid / get_validated_model) / shallow vs deep валідацію (is_valid / get_validated_model)
# - collections (list/set/tuple), dict-keys, Union (discriminated) / колекції (list/set/tuple), dict-ключі, Union (дискримінований)
# - Annotated-constraints
# - updating elastic_loaded_fields on assignment & deletion / оновлення elastic_loaded_fields при присвоєнні та видаленні

import pytest
#pytestmark = pytest.mark.filterwarnings("ignore:PydanticSerializationUnexpectedValue")

import sys
from enum import Enum
from typing import Annotated, Any, Dict, Generic, List, Literal, Optional, Tuple, TypeVar, Union

from pydantic import AliasChoices, AliasPath, Discriminator, EmailStr, Field, PrivateAttr, Tag, ValidationError, BaseModel, ConfigDict, create_model, field_validator, model_validator
# pydantic на Python < 3.12 приймає лише `TypedDict` з `typing_extensions`
from typing_extensions import TypedDict

from elasticmodel import ElasticModel


# --------------------------
# Models for tests
# --------------------------

class Created(ElasticModel):
    at: str
    by: str


class Password(ElasticModel):
    hash: str
    salt: str


class User(ElasticModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias="_id")
    first_name: str
    last_name: str
    middle_name: str | None = None
    email: EmailStr
    phone: str
    created: Created
    password: Password
    # Default field: важливо перевірити поведінку за defaults=False
    flag: bool = False


class UserSlim(ElasticModel):
    # Для списків користувачів (перевірка шляхів типу members[0].email)
    email: EmailStr
    created: Created


class Group(ElasticModel):
    name: str
    members: list[UserSlim]


# Для диктів з типізованими ключами/значеннями
class KV(ElasticModel):
    data: dict[int, float]


# Для колекцій і кортежів
class Containers(ElasticModel):
    nums_list: list[int]
    nums_set: set[int]
    pair_fixed: tuple[int, str]
    any_tail: tuple[int, ...]  # варіативний Tuple[T, ...]


# Для Annotated-constraints
class WithAnnotated(ElasticModel):
    short: Annotated[str, Field(min_length=3)]  # мін. довжина — 3


# Для Union (дискримінований)
class Card(ElasticModel):
    kind: Literal["card"]
    pan: str


class Bank(ElasticModel):
    kind: Literal["bank"]
    iban: str


class Payment(ElasticModel):
    method: Union[Card, Bank] = Field(..., discriminator="kind")


# --------------------------
# Основні тести ElasticModel
# --------------------------

def test_partial_creation_and_strict_access():
    """
    Часткове створення User з мінімального набору полів.
    Доступ до відсутніх полів → AttributeError (строгий доступ).
    """
    doc = {
        "_id": "u1",
        "first_name": "Ann",
        "last_name": "Lee",
        "created": {"at": "2025-08-15"},
        "password": {"hash": "h", "salt": "s"},
        "phone": "+1",
        "email": "ann@example.com",
    }
    u = User.elastic_create(doc, validate=True, defaults=False)

    # Завантажені поля читаються
    assert u.first_name == "Ann"
    assert u.created.at == "2025-08-15"

    # НЕЛОАДЕД вкладене поле → помилка
    with pytest.raises(AttributeError):
        _ = u.created.by

    # Дефолтне top-level поле, але за defaults=False → помилка
    with pytest.raises(AttributeError):
        _ = u.flag


def test_extra_fields_collected_in_elastic_extra():
    """
    Невідомі ключі мають складатися у .elastic_extra без валідації (навіть коли extra='ignore').
    """
    doc = {
        "_id": "u1",
        "first_name": "Ann",
        "last_name": "Lee",
        "created": {"at": "2025-08-15", "by": "sys"},
        "password": {"hash": "h", "salt": "s"},
        "phone": "+1",
        "email": "ann@example.com",
        "debug_flag": 1,  # зайве поле
    }
    u = User.elastic_create(doc, validate=False)
    assert u.elastic_extra["debug_flag"] == 1
    # .elastic_extra не впливає на схему
    assert "debug_flag" not in User.model_fields


def test_alias_support_for_id():
    """
    Перевірка alias: 'id' має ініціалізуватись із '_id'.
    """
    u = User.elastic_create({
        "_id": "x-1",
        "first_name": "A",
        "last_name": "B",
        "email": "a@b.com",
        "phone": "123",
        "created": {"at": "t", "by": "b"},
        "password": {"hash": "h", "salt": "s"},
    })
    assert u.id == "x-1"


def test_defaults_true_keeps_defaults_but_not_loaded():
    """
    defaults=True → підставити дефолти top-level полів і перевірити що elastic_is_loaded(default_field) == False.
    """
    doc = {
        "_id": "u1",
        "first_name": "Ann",
        "last_name": "Lee",
        "email": "ann@example.com",
        "phone": "+1",
        "created": {"at": "t", "by": "sys"},
        "password": {"hash": "h", "salt": "s"},
    }
    u = User.elastic_create(doc, defaults=True, validate=False)
    assert u.flag is False
    # дефолт не вважається "loaded"
    assert u.elastic_is_loaded("flag") is False


def test_nested_model_partial_and_strict_access():
    """
    Вкладена ElasticModel має таку ж семантику: partial + строгий доступ.
    """
    u = User.elastic_create({
        "_id": "u1",
        "first_name": "Ann",
        "last_name": "Lee",
        "email": "ann@example.com",
        "phone": "+1",
        "created": {"at": "2025-08-15"},           # без 'by'
        "password": {"hash": "h", "salt": "s"},
    })

    # Доступ до created.by → помилка
    with pytest.raises(AttributeError):
        _ = u.created.by


def test_is_valid_shallow_vs_deep_paths():
    """
    is_valid(recursive=True) має показати помилки у вкладених полях ('created.by').
    is_valid(recursive=False) перевіряє тільки верхній рівень і не лізе вглиб.
    """
    u = UserSlim.elastic_create({
        "email": "a@b.com",
        "created": {"at": "t"},  # немає 'by'
    })

    ok_deep, bad_deep = u.elastic_is_valid(recursive=True)
    assert ok_deep is False
    assert "created.by" in bad_deep

    ok_shallow, bad_shallow = u.elastic_is_valid(recursive=False)
    assert ok_shallow is True
    assert all(p != "created.by" for p in bad_shallow)


def test_get_validated_model_shallow_and_deep():
    """
    get_validated_model(recursive=False) не перевалідовує вкладені інстанси,
    отже пропустить відсутній created.by. А от recursive=True — кине ValidationError.
    """
    u = UserSlim.elastic_create({
        "email": "a@b.com",
        "created": {"at": "t"},  # без 'by'
    })

    # Shallow: працює (вкладений інстанс приймається «як є»)
    u_shallow = u.elastic_get_validated_model(recursive=False)
    assert isinstance(u_shallow, UserSlim)

    # Deep: має впасти
    with pytest.raises(ValidationError):
        _ = u.elastic_get_validated_model(recursive=True)


def test_dict_key_and_value_coercion_validate_true_and_false():
    """
    dict[int, float]:
    - validate=True → ключі та значення коерсяться (ключ '1' → 1, '2.5' → 2.5)
    - validate=False → лишаються «як є»
    """
    d = {"data": {"1": "2.5", "2": 3}}
    m_true = KV.elastic_create(d, validate=True)
    assert m_true.data == {1: 2.5, 2: 3.0}

    m_false = KV.elastic_create(d, validate=False)
    assert m_false.data == {"1": "2.5", "2": 3}


def test_list_and_set_coercion():
    """
    list[int] і set[int]:
    - validate=True → елементи приводяться до int
    - validate=False → залишаються як є
    """
    c_true = Containers.elastic_create({
        "nums_list": ["1", 2, "3"],
        "nums_set":  {"1", 2, "3"},
        "pair_fixed": [1, "x"],
        "any_tail":  ["4", "5", 6],
    }, validate=True)

    assert c_true.nums_list == [1, 2, 3]
    assert c_true.nums_set == {1, 2, 3}
    assert c_true.pair_fixed == (1, "x")
    assert c_true.any_tail == (4, 5, 6)

    c_false = Containers.elastic_create({
        "nums_list": ["1", 2, "3"],
        "nums_set":  {"1", 2, "3"},
        "pair_fixed": [1, "x"],
        "any_tail":  ["4", "5", 6],
    }, validate=False)

    assert c_false.nums_list == ["1", 2, "3"]
    assert c_false.nums_set == {"1", 2, "3"}
    assert c_false.pair_fixed == (1, "x")
    assert c_false.any_tail == ("4", "5", 6)


def test_tuple_fixed_validate_true_and_false_no_truncation():
    """
    pair_fixed: tuple[int, str]
    - validate=True і невірна довжина → делегуємо у Pydantic (ValidationError)
    - validate=False і довша послідовність → коерсимо тільки відомі позиції, хвіст не обрізаємо
    """
    with pytest.raises(ValidationError):
        _ = Containers.elastic_create({
            "nums_list": [],
            "nums_set": set(),
            "pair_fixed": [1, "x", "EXTRA"],  # довжина 3 замість 2
            "any_tail": [],
        }, validate=True)

    c = Containers.elastic_create({
        "nums_list": [],
        "nums_set": set(),
        "pair_fixed": ["1", "x", 99],  # три елементи
        "any_tail": [],
    }, validate=False)

    assert c.pair_fixed == ("1", "x", 99)


def test_discriminated_union_validate_true():
    """
    Union із дискримінатором: validate=True → будується коректний підтип.
    """
    p = Payment.elastic_create({
        "method": {"kind": "card", "pan": "1234", "xxx": 1}
    }, validate=True)

    assert isinstance(p.method, Card)
    assert p.method.pan == "1234"


def test_discriminated_union_validate_false_builds_branch():
    """
    Union із дискримінатором: validate=False → будується модель гілки за тегом, значення «як є».
    """
    p = Payment.elastic_create({
        "method": {"kind": "bank", "iban": "UA..."}
    }, validate=False)

    assert isinstance(p.method, Bank)
    assert p.method.elastic_loaded_fields == {"kind", "iban"}
    assert p.method.iban == "UA..."


def test_annotated_constraints_preserved():
    """
    Annotated[str, Field(min_length=3)]:
    - validate=True → має врахувати constraint (мін. довжина) і впасти для "ac".
    - validate=False → пропускаємо як є.
    """
    with pytest.raises(ValidationError):
        _ = WithAnnotated.elastic_create({"short": "ac"}, validate=True)

    m = WithAnnotated.elastic_create({"short": "ab"}, validate=False)
    assert m.short == "ab"


def test_manual_assignment_updates_loaded_and_deletion_removes_from_loaded():
    """
    Присвоєння має позначати поле як «завантажене», а `del` — прибирати з loaded.
    """
    u = UserSlim.elastic_create({
        "created": {"at": "t", "by": "sys"}
    }, validate=False, defaults=False)

    assert u.elastic_is_loaded("email") is False
    with pytest.raises(AttributeError):
        _ = u.email

    # Присвоєння → loaded
    u.email = "x@y.z"
    assert u.elastic_is_loaded("email") is True
    assert u.email == "x@y.z"

    # Видалення → не loaded
    del u.email
    assert u.elastic_is_loaded("email") is False
    with pytest.raises(AttributeError):
        _ = u.email


def test_group_members_paths_in_deep_validation():
    """
    Перевірка форматування шляхів у масивах: members[1].created.by.
    """
    g = Group.elastic_create({
        "name": "admins",
        "members": [
            {"email": "ok@ex.com", "created": {"at": "t", "by": "sys"}},
            {"email": "bad@ex.com", "created": {"at": "t"}},   # немає 'by'
        ]
    }, validate=False)

    ok_shallow, bad_shallow = g.elastic_is_valid(recursive=False)
    assert ok_shallow is True

    ok_deep, bad_deep = g.elastic_is_valid(recursive=True)
    assert ok_deep is False
    assert "members[1].created.by" in bad_deep


# --------------------------
# Порівняння з pydantic.BaseModel
# --------------------------

class PydCreated(BaseModel):
    at: str
    by: str


class PydUserSlim(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    email: EmailStr
    created: PydCreated


class PydUserBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra='ignore')
    id: str = Field(alias="_id")
    email: EmailStr


def test_basemodel_partial_creation_vs_elasticmodel():
    """
    BaseModel: часткове створення падає з ValidationError.
    ElasticModel: створюється частково і дозволяє shallow-валідацію.
    """
    partial_doc = {"email": "a@b.com", "created": {"at": "t"}}  # без created.by

    with pytest.raises(ValidationError):
        _ = PydUserSlim.model_validate(partial_doc)

    u = UserSlim.elastic_create(partial_doc, validate=False)
    ok_shallow, _ = u.elastic_is_valid(recursive=False)
    assert ok_shallow is True
    ok_deep, bad_deep = u.elastic_is_valid(recursive=True)
    assert ok_deep is False and "created.by" in bad_deep


def test_basemodel_extra_ignored_vs_elasticmodel_extra_captured():
    """
    BaseModel за замовчуванням ігнорує extra (extra='ignore'), але їх не зберігає.
    ElasticModel зберігає extra у .elastic_extra без валідації.
    """
    doc = {
        "_id": "u1",
        "email": "a@b.com",
        "debug_flag": 1,
    }

    pyd = PydUserBase.model_validate(doc)
    assert not hasattr(pyd, "debug_flag")

    class EMUser(ElasticModel):
        id: str = Field(alias="_id")
        email: EmailStr

    eu = EMUser.elastic_create(doc, validate=False)
    assert eu.elastic_extra["debug_flag"] == 1


def test_basemodel_deep_validation_always_vs_elasticmodel_shallow():
    """
    BaseModel завжди перевіряє глибину і впаде, якщо у вкладеній моделі бракує поля.
    ElasticModel може пройти shallow-валідацію, але впаде на deep.
    """
    partial_doc = {"email": "a@b.com", "created": {"at": "t"}}  # без created.by

    with pytest.raises(ValidationError):
        _ = PydUserSlim.model_validate(partial_doc)

    u = UserSlim.elastic_create(partial_doc, validate=False)
    u_shallow = u.elastic_get_validated_model(recursive=False)
    assert isinstance(u_shallow, UserSlim)
    with pytest.raises(ValidationError):
        _ = u.elastic_get_validated_model(recursive=True)


# --------------------------
# Додаткові нові тести
# --------------------------

def test_elastic_loaded_fields_with_extra_allow_and_deletion():
    """
    extra='allow': unknowns доступні через ".", тому з'являються в elastic_loaded_fields;
    після delattr — зникають і з моделі, і з elastic_loaded_fields.
    """
    class UAllow(ElasticModel):
        model_config = ConfigDict(extra='allow')
        a: int

    u = UAllow.elastic_create({"a": 1, "debug": 42}, validate=False)
    # доступний як атрибут і в loaded
    assert getattr(u, "debug") == 42
    assert "debug" in u.elastic_loaded_fields

    del u.debug
    assert not hasattr(u, "debug")
    assert "debug" not in u.elastic_loaded_fields


@pytest.mark.parametrize("extra", ["ignore", "allow", "forbid"])
def test_elastic_extra_always_captures_unknowns(extra):
    """
    .elastic_extra зберігає невідомі ключі незалежно від налаштування extra.
    При `extra='forbid'` це розширення: pydantic відхилив би такі дані, а `elastic_create` кладе ключ в `elastic_extra` без помилки.
    """
    class Unknowns(ElasticModel):
        model_config = ConfigDict(extra=extra)
        a: int

    m = Unknowns.elastic_create({"a": 1, "u": 2}, validate=False)

    assert m.a == 1
    assert m.elastic_extra == {"u": 2}


def test_strict_validate_blocks_coercion():
    """
    strict_validate=True → без коерсингу, отже рядок у полі int має впасти.
    """
    class OnlyInt(ElasticModel):
        x: int

    with pytest.raises(ValidationError):
        _ = OnlyInt.elastic_create({"x": "1"}, validate=True, strict_validate=True)
#

def test_attributes():
    class EM(ElasticModel):
        _id: Any = PrivateAttr(default=None)
        x: int

    class BM(BaseModel):
        _id: Any = PrivateAttr(default=None)
        x: int

    em_1 = EM.elastic_create({})
    em_1.x = 22
    assert em_1.x == 22
    assert em_1._id == None
    em_1._id = 11
    assert em_1._id == 11

    em_2 = EM(x=22)
    assert em_2.x == 22
    assert em_2._id == None
    em_2._id = 11
    assert em_2._id == 11

    bm_2 = BM(x=22)
    assert bm_2.x == 22
    assert bm_2._id == None
    bm_2._id = 11
    assert bm_2._id == 11


# --------------------------
# Публічний контракт: NotLoadedFieldError
# --------------------------

def test_not_loaded_field_error_exported_from_package():
    """
    README інструктує імпортувати тип помилки з кореня пакета, тому цей імпорт є частиною публічного контракту.

    Імпорт зроблено локальним навмисно: на рівні модуля відсутній експорт обриває збирання всього файлу,
    і замість одного цільового падіння отримуємо помилку collection на всіх тестах набору.
    """
    from elasticmodel import NotLoadedFieldError

    # Наявні тести очікують AttributeError на незавантаженому полі, тому базовий клас входить у контракт.
    assert issubclass(NotLoadedFieldError, AttributeError)


def test_unloaded_declared_field_raises_not_loaded_field_error():
    """
    Оголошене поле, якого не було в даних при `defaults=False`, фізично відсутнє в інстансі,
    тому його читання має давати помилку бібліотеки, а не штатну помилку доступу.
    """
    from elasticmodel import NotLoadedFieldError

    u = UserSlim.elastic_create({"created": {"at": "t", "by": "sys"}}, validate=False, defaults=False)

    with pytest.raises(NotLoadedFieldError):
        _ = u.email


def test_undeclared_name_keeps_standard_access_behavior():
    """
    Неоголошене ім'я перехоплювач пропускає далі: при `extra='allow'` повертається значення,
    при `extra='ignore'` лишається штатна помилка доступу — і вона не є помилкою бібліотеки.
    """
    from elasticmodel import NotLoadedFieldError

    class UIgnore(ElasticModel):
        model_config = ConfigDict(extra='ignore')
        a: int

    class UAllow(ElasticModel):
        model_config = ConfigDict(extra='allow')
        a: int

    ua = UAllow.elastic_create({"a": 1, "unknown": 2}, validate=False)
    assert ua.unknown == 2

    ui = UIgnore.elastic_create({"a": 1, "unknown": 2}, validate=False)
    with pytest.raises(AttributeError) as exc_info:
        _ = ui.unknown
    assert not isinstance(exc_info.value, NotLoadedFieldError)


def test_not_loaded_field_error_message_names_the_field():
    """
    NFR-3: повідомлення називає поле. Окремий тест, бо doctest під `IGNORE_EXCEPTION_DETAIL` текст повідомлення ігнорує.
    """
    from elasticmodel import NotLoadedFieldError

    u = UserSlim.elastic_create({"created": {"at": "t", "by": "sys"}}, validate=False, defaults=False)

    with pytest.raises(NotLoadedFieldError) as exc_info:
        _ = u.email
    assert "email" in str(exc_info.value)


def test_interceptor_not_executed_when_reading_loaded_field():
    """
    NFR-2 перевіряється структурно: перехоплювач спрацьовує лише після невдалого звичайного пошуку,
    тому читання завантаженого поля не виконує його коду.
    """
    from elasticmodel import NotLoadedFieldError

    intercepted: list[str] = []

    class Counted(ElasticModel):
        a: int
        b: int

        def __getattr__(self, name: str) -> Any:
            intercepted.append(name)
            return super().__getattr__(name)

    m = Counted.elastic_create({"a": 1}, validate=False, defaults=False)
    intercepted.clear()  # Конструктор читає службові PrivateAttr тим самим механізмом, тому рахуємо лише звернення після побудови.

    assert m.a == 1
    assert intercepted == []

    with pytest.raises(NotLoadedFieldError):
        _ = m.b
    assert intercepted == ["b"]


def test_guarantee_boundary_hasattr_and_getattr_with_default():
    """
    Межа гарантії 1: успадкування від штатної помилки доступу означає, що перевірка наявності атрибута
    дає «немає», а читання з дефолтом віддає дефолт. Обидва способи глушать порушення контракту.
    """
    u = UserSlim.elastic_create({"created": {"at": "t", "by": "sys"}}, validate=False, defaults=False)

    assert hasattr(u, "email") is False
    assert getattr(u, "email", "fallback") == "fallback"


def test_guarantee_boundary_field_shadowing_parent_attribute():
    """
    Межа гарантії 2: поле з іменем атрибута батьківського класу знаходить звичайний пошук, тому перехоплювач
    не викликається і помилки немає. pydantic попереджає про таке затінення на етапі оголошення класу.
    """
    class Shadowing(ElasticModel):
        copy: str

    m = Shadowing.elastic_create({}, validate=False, defaults=False)

    assert callable(m.copy)


# --------------------------
# Публічний контракт: elastic_loaded_fields
# --------------------------

def test_loaded_fields_exclude_library_service_names():
    """
    Перелік має містити лише завантажені дані, без службових імен бібліотеки.
    Колись туди протікав службовий прапорець завершення; тест охороняє від повернення цього дефекту.
    """
    u = UserSlim.elastic_create({"email": "a@b.com", "created": {"at": "t", "by": "s"}}, validate=False)

    assert [name for name in u.elastic_loaded_fields if name.startswith("_elastic")] == []


def test_loaded_fields_mutation_does_not_change_state():
    """
    Зміна отриманого набору не може зробити так, щоб `elastic_is_loaded` брехав.
    """
    u = UserSlim.elastic_create({"email": "a@b.com", "created": {"at": "t", "by": "s"}}, validate=False)
    assert u.elastic_is_loaded("email") is True

    u.elastic_loaded_fields.discard("email")

    assert u.elastic_is_loaded("email") is True


def test_loaded_fields_keep_data_keys_including_underscore_with_extra_allow():
    """
    Ключ із підкресленням, що прийшов у даних при `extra='allow'`, лежить у `model_extra`.
    Це завантажені дані, а не приватний атрибут, тому ключ лишається в переліку.
    """
    class UAllow(ElasticModel):
        model_config = ConfigDict(extra='allow')
        a: int

    u = UAllow.elastic_create({"a": 1, "plain": 2, "_under": 3}, validate=False)

    loaded = u.elastic_loaded_fields
    assert "plain" in loaded
    assert "_under" in loaded


# --------------------------
# Публічний контракт: правила аліасів як у pydantic
# --------------------------

ALIAS_CONFIGS = [
    pytest.param(ConfigDict(), id="no-keys"),
    pytest.param(ConfigDict(validate_by_name=True), id="validate_by_name"),
    pytest.param(ConfigDict(populate_by_name=True), id="populate_by_name"),
    pytest.param(ConfigDict(validate_by_name=True, validate_by_alias=False), id="name-only"),
]


def _pydantic_accepts_key(config: ConfigDict, key: str) -> bool:
    """
    Еталон для тестів аліасів: чи заповнює pydantic поле `id` (аліас `_id`) з ключа `key` при цьому конфігу.
    """
    class Reference(BaseModel):
        model_config = config
        id: str = Field(alias="_id")

    try:
        Reference.model_validate({key: "x-1"})
        return True
    except ValidationError:
        return False


@pytest.mark.parametrize("key", ["_id", "id"])
@pytest.mark.parametrize("config", ALIAS_CONFIGS)
def test_alias_rules_match_pydantic(config, key):
    """
    Вхідний ключ заповнює поле тоді й лише тоді, коли його приймає pydantic з тим самим конфігом.
    Ключ, який pydantic відкидає, потрапляє у сховище невідомих полів, а поле лишається незавантаженим.
    """
    class AliasModel(ElasticModel):
        model_config = config
        id: str = Field(alias="_id")

    m = AliasModel.elastic_create({key: "x-1"})

    if _pydantic_accepts_key(config, key):
        assert m.id == "x-1"
        assert key not in m.elastic_extra
    else:
        assert not m.elastic_is_loaded("id")
        assert m.elastic_extra[key] == "x-1"


def test_forbidden_key_does_not_fill_field_with_default():
    """
    Ім'я аліасованого поля без ключів конфігу заборонене, тому воно не заповнює поле в обхід конфігу.
    Межовий випадок — поле з дефолтом: заборонений ключ не має позначити цей дефолт завантаженим.
    """
    class WithDefault(ElasticModel):
        code: str = Field("-", alias="_code")

    m = WithDefault.elastic_create({"code": "c"})

    assert not m.elastic_is_loaded("code")
    assert m.elastic_extra == {"code": "c"}


@pytest.mark.parametrize(
    "data",
    [{"_id": "A", "id": "N"}, {"id": "N", "_id": "A"}],
    ids=["alias-first", "name-first"],
)
def test_alias_wins_over_field_name(data):
    """
    Якщо для поля прийшли і аліас, і ім'я, поле отримує значення аліасу незалежно від порядку ключів, як у pydantic.
    Ключ, що програв, не губиться: він потрапляє у сховище невідомих полів.
    """
    class BothKeys(ElasticModel):
        model_config = ConfigDict(validate_by_name=True)
        id: str = Field(alias="_id")

    m = BothKeys.elastic_create(data)

    assert m.id == "A"
    assert m.elastic_extra == {"id": "N"}


def test_validation_methods_agree_for_model_built_by_alias():
    """
    Модель без ключів конфігу будується за аліасом, а payload валідації складається з імен полів.
    Обидва методи валідації мають прийняти такий payload, інакше `elastic_is_valid` бреше про відсутнє поле.
    """
    class AliasOnly(ElasticModel):
        id: str = Field(alias="_id")
        name: str

    m = AliasOnly.elastic_create({"_id": "u1", "name": "Ann"})

    assert m.elastic_is_valid(recursive=True) == (True, [])
    assert m.elastic_is_valid(recursive=False) == (True, [])
    assert m.elastic_get_validated_model().id == "u1"


@pytest.mark.parametrize("key", ["_id", "id"])
@pytest.mark.parametrize("config", ALIAS_CONFIGS)
def test_nested_basemodel_alias_rules_match_pydantic(config, key):
    """
    При `validate=False` вкладений `BaseModel` будує бібліотека, а не pydantic, тому правила аліасів мають збігатися з pydantic і тут.
    Інакше той самий вкладений клас приймав би різні ключі залежно від прапорця `validate`.
    """
    class Inner(BaseModel):
        model_config = config
        id: str = Field(alias="_id")

    class Outer(ElasticModel):
        inner: Inner

    m = Outer.elastic_create({"inner": {key: "x-1"}}, validate=False)

    if _pydantic_accepts_key(config, key):
        assert m.inner.id == "x-1"
    else:
        assert "id" not in m.inner.model_fields_set


VALIDATION_ALIAS_FIELDS = {
    "string": lambda: Field(0, alias="_a", validation_alias="_v"),
    "choices": lambda: Field(0, alias="_a", validation_alias=AliasChoices("_v", AliasPath("meta", 0))),
    "path": lambda: Field(0, alias="_a", validation_alias=AliasPath("meta", 0)),
    "no-alias": lambda: Field(0, validation_alias="_v"),
}


def _validation_alias_class(base: type[BaseModel], field_kind: str, config: ConfigDict = ConfigDict()) -> type[BaseModel]:
    """
    Клас з полем `x`, що має власний `validation_alias`, оголошений на `base`.
    Дефолт поля потрібен, щоб pydantic прийняв дані, в яких поле не заповнене.
    """
    class ValidationAliasModel(base):
        model_config = config
        x: int = VALIDATION_ALIAS_FIELDS[field_kind]()

    return ValidationAliasModel


@pytest.mark.parametrize("key", ["x", "_a"])
@pytest.mark.parametrize("config", ALIAS_CONFIGS)
@pytest.mark.parametrize("field_kind", VALIDATION_ALIAS_FIELDS)
def test_validation_alias_field_key_rules_match_pydantic(field_kind, config, key):
    """
    Для поля з власним `validation_alias` ім'я й `alias` заповнюють поле тоді й лише тоді, коли їх приймає pydantic з тим самим конфігом.
    pydantic валідує таке поле лише за `validation_alias` і, з `validate_by_name`, за іменем, тож `alias` не приймається за жодного конфігу.
    """
    accepted = _validation_alias_class(BaseModel, field_kind, config).model_validate({key: 7}).x == 7

    m = _validation_alias_class(ElasticModel, field_kind, config).elastic_create({key: 7})

    assert m.elastic_is_loaded("x") == accepted
    if not accepted:
        assert m.elastic_extra == {key: 7}


@pytest.mark.parametrize("defaults", [False, True])
@pytest.mark.parametrize(
    "field_kind, data",
    [
        ("string", {"_v": "bad"}),
        ("choices", {"_v": "bad"}),
        ("choices", {"meta": ["bad"]}),
        ("path", {"meta": ["bad"]}),
        ("no-alias", {"_v": "bad"}),
    ],
)
def test_validation_alias_key_does_not_fill_field(field_kind, data, defaults):
    """
    Ключ `validation_alias` поле не заповнює: читання таких ключів поза scope, а `model_construct` заповнив би ним поле без валідації.
    Значення лежить в `elastic_extra`, поле не завантажене, а з `defaults=True` у полі дефолт, а не невалідне значення.
    """
    m = _validation_alias_class(ElasticModel, field_kind).elastic_create(data, defaults=defaults)

    assert not m.elastic_is_loaded("x")
    assert m.elastic_extra == data
    if defaults:
        assert m.x == 0


# --------------------------
# Публічний контракт: сховище невідомих полів і облік завантажених полів
# --------------------------

def test_elastic_extra_mutation_does_not_change_model():
    """
    Властивість віддає копію, тому зміна отриманого словника не змінює модель.
    """
    m = Created.elastic_create({"at": "t", "unknown": 1})

    extra = m.elastic_extra
    extra.clear()
    extra["injected"] = 2

    assert m.elastic_extra == {"unknown": 1}


def test_models_created_by_validation_know_loaded_fields():
    """
    Модель, яку створила валідація pydantic, вважає завантаженими поля з вхідних даних — на кожному рівні вкладеності.
    Поле, заповнене дефолтом під час валідації, завантаженим не вважається: та сама семантика, що в `elastic_create`.
    Після створення облік триває: присвоєння додає поле до переліку.
    """
    class Stamp(ElasticModel):
        at: str
        by: str = "system"

    class Owner(ElasticModel):
        id: str
        name: str
        created: Stamp

    src = Owner.elastic_create({"id": "1", "name": "Ann", "created": {"at": "2025"}})
    validated = src.elastic_get_validated_model(recursive=True)
    constructed = Owner(id="1", name="Ann", created=Stamp(at="2025"))

    for m in (validated, constructed):
        assert m.elastic_loaded_fields == {"id", "name", "created"}
        assert m.created.elastic_loaded_fields == {"at"}

    validated.created.by = "me"
    assert validated.created.elastic_is_loaded("by")


def test_shallow_validation_does_not_change_source_loaded_fields():
    """
    Shallow-перевірка передає вкладену модель у pydantic як є, і pydantic знову запускає на ній валідатори моделі.
    Облік завантажених полів вихідної моделі від цього змінюватися не повинен, зокрема не повинно повернутися видалене поле.
    """
    class Stamp(ElasticModel):
        at: str
        by: str = "system"

    class Owner(ElasticModel):
        created: Stamp

    src = Owner.elastic_create({"created": {"at": "2025", "by": "me"}})
    del src.created.by

    src.elastic_is_valid(recursive=False)

    assert src.created.elastic_loaded_fields == {"at"}


# --------------------------
# Публічний контракт: параметри конструктора у вкладених моделях
# --------------------------

def test_defaults_reach_nested_models():
    """
    `defaults=True` діє на кожному рівні вкладеності, зокрема в елементах списку.
    Дефолт вкладеної моделі читається, але не вважається завантаженим — та сама семантика, що на верхньому рівні.
    """
    class Stamp(ElasticModel):
        at: str
        by: str = "system"

    class Owner(ElasticModel):
        created: Stamp
        history: list[Stamp]

    m = Owner.elastic_create({"created": {"at": "1"}, "history": [{"at": "2"}]}, defaults=True)

    assert m.created.by == "system"
    assert not m.created.elastic_is_loaded("by")
    assert m.history[0].by == "system"
    assert not m.history[0].elastic_is_loaded("by")


def test_strict_validate_reaches_nested_models():
    """
    `strict_validate=True` вимикає коерсинг і у вкладеній моделі: рядок не стає числом.
    Без прапорця той самий рядок коерситься.
    """
    class Counter(ElasticModel):
        n: int

    class Holder(ElasticModel):
        counter: Counter

    assert Holder.elastic_create({"counter": {"n": "5"}}).counter.n == 5

    with pytest.raises(ValidationError):
        Holder.elastic_create({"counter": {"n": "5"}}, strict_validate=True)


# --------------------------
# Публічний контракт: анотації публічних методів у рантаймі
# --------------------------

def test_public_method_annotations_resolve_at_runtime():
    """
    Інструменти, що читають анотації в рантаймі (`validate_call`, генератори документації), мають розв'язувати повернення публічних методів.
    Тест іде і в прогін на Python 3.10, де `Self` у `typing` ще немає.
    """
    import typing

    from typing_extensions import Self

    for method in (ElasticModel.elastic_create, ElasticModel.elastic_get_validated_model):
        assert typing.get_type_hints(method)["return"] is Self


# --------------------------
# Відповідність BaseModel: модель зі звичайного шляху pydantic
# --------------------------

def _pair_class(base: type[BaseModel], **config) -> type[BaseModel]:
    """
    Той самий клас, оголошений на `base`: еталон — `BaseModel`, перевірюваний — `ElasticModel`.
    """
    class Pair(base):
        model_config = ConfigDict(**config)
        a: int
        b: int = 2

    return Pair


def _assigned_default_value(cls):
    m = cls(a=1)
    m.b = 2
    return m, cls(a=1)


EQUALITY_PAIRS = [
    pytest.param(lambda cls: (cls(a=1), cls(a=1, b=2)), {}, id="default-vs-explicit"),
    pytest.param(lambda cls: (cls(a=1), cls(a=1, b=3)), {}, id="different-data"),
    pytest.param(lambda cls: (cls.model_construct(a=1), cls(a=1)), {}, id="construct-vs-init"),
    pytest.param(lambda cls: (cls(a=1).model_copy(), cls(a=1, b=2)), {}, id="copy-vs-init"),
    pytest.param(_assigned_default_value, {}, id="assigned-default-value"),
    pytest.param(lambda cls: (cls(a=1), cls(a=1, b=2)), {"frozen": True}, id="frozen"),
]


@pytest.mark.parametrize("make_pair, config", EQUALITY_PAIRS)
def test_equality_matches_pydantic(make_pair, config):
    """
    `==` для моделей зі звичайного шляху pydantic дає той самий результат, що й для того самого класу на `BaseModel`.
    pydantic порівнює і приватні атрибути, тому стан бібліотеки, що різниться для однакових даних, ламав би цю рівність.
    """
    left, right = make_pair(_pair_class(BaseModel, **config))
    elastic_left, elastic_right = make_pair(_pair_class(ElasticModel, **config))

    assert (elastic_left == elastic_right) == (left == right)


def test_loaded_fields_follow_model_state_after_copies_and_special_paths():
    """
    Облік завантажених полів відповідає стану моделі на шляхах, що змінюють модель в обхід її перехоплювачів.
    - поверхнева копія не ділить облік з оригіналом;
    - `model_copy(update=...)` і `model_construct` позначають передані поля завантаженими;
    - присвоєння через сетер `@property` не додає в облік ім'я властивості.
    """
    class Doc(ElasticModel):
        x: int
        y: int = 0

        @property
        def double_x(self) -> int:
            return self.x * 2

        @double_x.setter
        def double_x(self, value: int) -> None:
            self.x = value // 2

    original = Doc.elastic_create({"x": 1})
    copied = original.model_copy()
    copied.y = 5
    assert copied.elastic_is_loaded("y")
    assert not original.elastic_is_loaded("y")

    updated = Doc.elastic_create({"x": 1}).model_copy(update={"y": 5})
    assert updated.y == 5
    assert updated.elastic_is_loaded("y")

    assert Doc.model_construct(x=1).elastic_is_loaded("x")

    m = Doc(x=1)
    m.double_x = 10
    assert m.x == 5
    assert m.elastic_loaded_fields == {"x"}


def test_nested_model_validation_errors_match_pydantic():
    """
    Помилки валідації вкладеної моделі мають той самий заголовок, тип і шлях, що й для того самого класу на `BaseModel`.
    Механізм бібліотеки не повинен з'являтися в тексті помилок, які бачить користувач.
    """
    from pydantic import TypeAdapter

    def errors_for(base: type[BaseModel]):
        class Leaf(base):
            n: int

        with pytest.raises(ValidationError) as caught:
            TypeAdapter(Union[Leaf, int]).validate_python({"n": "x"})
        return caught.value.title, [(e["type"], e["loc"]) for e in caught.value.errors()]

    assert errors_for(ElasticModel) == errors_for(BaseModel)


def test_elastic_create_with_serialize_by_alias_matches_pydantic():
    """
    Налаштування серіалізації не впливають на побудову моделі: повні дані дають те саме, що `model_validate`,
    а аліасоване поле вважається завантаженим під своїм іменем.
    """
    def build(base: type[BaseModel]):
        class Account(base):
            model_config = ConfigDict(serialize_by_alias=True)
            id: str = Field(alias="_id")
            name: str = "-"

        return Account

    data = {"_id": "1", "name": "Ann"}

    reference = build(BaseModel).model_validate(data)
    elastic = build(ElasticModel).elastic_create(data)

    assert elastic.model_dump() == reference.model_dump()
    assert elastic.elastic_loaded_fields == {"id", "name"}


def test_elastic_create_on_frozen_model():
    """
    `elastic_create` будує frozen-модель: незавантажений дефолт прибирається без присвоєння, яке frozen-модель забороняє.
    """
    from elasticmodel import NotLoadedFieldError

    class Frozen(ElasticModel):
        model_config = ConfigDict(frozen=True)
        x: int
        y: int = 0

    m = Frozen.elastic_create({"x": 1})

    assert m.x == 1
    assert not m.elastic_is_loaded("y")
    with pytest.raises(NotLoadedFieldError):
        m.y


def test_equality_counts_elastic_extra():
    """
    Модель з `elastic_create` — уже не те саме, що `BaseModel`: `==` враховує її `elastic_extra`.
    Моделі без невідомих ключів це не зачіпає, тому вона дорівнює моделі з конструктора з тими самими даними.
    """
    class Item(ElasticModel):
        a: int

    assert Item.elastic_create({"a": 1, "x": 9}) != Item.elastic_create({"a": 1})
    assert Item.elastic_create({"a": 1}) == Item(a=1)


# --------------------------
# Відповідність BaseModel: перевірка моделі
# --------------------------

@pytest.mark.parametrize("recursive", [True, False], ids=["deep", "shallow"])
def test_validation_does_not_take_field_value_from_model_extra(recursive):
    """
    При `extra='allow'` ключ з іменем аліасованого поля, що програв аліасу, pydantic кладе в `model_extra`.
    Перевірка не повинна брати з нього значення поля: копія має те саме поле й той самий `model_extra`, що й pydantic.
    Коли саме поле видалене, такий ключ не повинен його підмінити — модель невалідна.
    """
    def build(base: type[BaseModel]):
        class Account(base):
            model_config = ConfigDict(extra="allow")
            uid: str = Field(alias="_id")

        return Account

    data = {"_id": "REAL", "uid": "FAKE"}
    reference = build(BaseModel).model_validate(data)
    m = build(ElasticModel).model_validate(data)

    validated = m.elastic_get_validated_model(recursive=recursive)
    assert (validated.uid, validated.model_extra) == (reference.uid, reference.model_extra)

    del m.uid
    assert m.elastic_is_valid(recursive=recursive) == (False, ["uid"])


def _serialized_by_alias(base: type[BaseModel]):
    class Account(base):
        model_config = ConfigDict(serialize_by_alias=True)
        uid: str = Field(alias="_id")

    return Account, {"_id": "1"}


def _excluded_from_dump(base: type[BaseModel]):
    class Account(base):
        secret: str = Field(exclude=True)

    return Account, {"secret": "s"}


def _custom_serializer(base: type[BaseModel]):
    from pydantic import field_serializer

    class Counter(base):
        n: int

        @field_serializer("n")
        def _show_n(self, value: int) -> str:
            return f"#{value}"

    return Counter, {"n": 1}


def _computed_with_forbid(base: type[BaseModel]):
    from pydantic import computed_field

    class Square(base):
        model_config = ConfigDict(extra="forbid")
        side: int

        @computed_field
        @property
        def area(self) -> int:
            return self.side * self.side

    return Square, {"side": 2}


@pytest.mark.parametrize(
    "build",
    [_serialized_by_alias, _excluded_from_dump, _custom_serializer, _computed_with_forbid],
    ids=["serialize_by_alias", "exclude", "field_serializer", "computed_field"],
)
def test_deep_validation_ignores_serialization_settings(build):
    """
    Налаштування виводу моделі не впливають на перевірку: модель, яку pydantic вважає валідною, валідна й для глибокої перевірки.
    """
    reference_cls, data = build(BaseModel)
    reference_cls.model_validate(data)

    elastic_cls, _ = build(ElasticModel)
    m = elastic_cls.elastic_create(data)

    assert m.elastic_is_valid(recursive=True) == (True, [])


def test_elastic_create_supports_computed_field():
    """
    `elastic_create` будує модель з `@computed_field`, бо більше не серіалізує модель під час створення.
    З повними даними `model_dump()` збігається з pydantic.
    Обчислюване поле, що читає незавантажене поле, дає `NotLoadedFieldError`, як і будь-який інший код, що читає таке поле.
    """
    from pydantic import computed_field

    from elasticmodel import NotLoadedFieldError

    def build(base: type[BaseModel]) -> type[BaseModel]:
        class Person(base):
            first: str
            last: str

            @computed_field
            @property
            def full(self) -> str:
                return f"{self.first} {self.last}"

        return Person

    data = {"first": "Ann", "last": "Lee"}
    assert build(ElasticModel).elastic_create(data).model_dump() == build(BaseModel).model_validate(data).model_dump()

    partial = build(ElasticModel).elastic_create({"first": "Ann"})

    assert partial.elastic_loaded_fields == {"first"}
    with pytest.raises(NotLoadedFieldError):
        partial.full


@pytest.mark.parametrize("recursive", [True, False], ids=["deep", "shallow"])
def test_validated_copy_keeps_elastic_extra(recursive):
    """
    Копія з `elastic_get_validated_model` переносить `elastic_extra` джерела на кожному рівні вкладеності, зокрема в елементах списку.
    Тому валідна модель дорівнює своїй копії: `==` враховує `elastic_extra`.
    """
    class Stamp(ElasticModel):
        at: str

    class Owner(ElasticModel):
        name: str
        created: Stamp
        history: list[Stamp]

    src = Owner.elastic_create({
        "name": "Ann",
        "zz": 1,
        "created": {"at": "t", "yy": 2},
        "history": [{"at": "h", "ww": 3}],
    })

    validated = src.elastic_get_validated_model(recursive=recursive)

    assert validated.elastic_extra == {"zz": 1}
    assert validated.created.elastic_extra == {"yy": 2}
    assert validated.history[0].elastic_extra == {"ww": 3}
    assert validated == src


UNFILLED_KEY_CASES = [
    pytest.param({}, {"id": "N", "x": 1}, id="forbidden-name"),
    pytest.param({"validate_by_name": True, "validate_by_alias": False}, {"_id": "A", "x": 1}, id="forbidden-alias"),
    pytest.param({"validate_by_name": True}, {"_id": "A", "id": "N", "x": 1}, id="name-lost-to-alias"),
]


def _unfilled_key_class(base: type[BaseModel], **config) -> type[BaseModel]:
    """
    Клас з аліасованим полем при `extra='allow'`, оголошений на `base`.
    Дефолт поля потрібен, щоб pydantic прийняв дані, в яких поле не заповнене.
    """
    class Keys(base):
        model_config = ConfigDict(extra="allow", **config)
        id: str = Field("default", alias="_id")

    return Keys


@pytest.mark.parametrize("config, data", UNFILLED_KEY_CASES)
def test_unfilled_key_lands_in_model_extra_as_in_pydantic(config, data):
    """
    При `extra='allow'` pydantic кладе в `model_extra` і ключ, що не заповнив поле: заборонений конфігом або той, що програв аліасу.
    `elastic_create` дає той самий `model_extra`, а поле заповнює лише тоді, коли його заповнює pydantic.
    """
    from elasticmodel import NotLoadedFieldError

    reference = _unfilled_key_class(BaseModel, **config).model_validate(data)
    filled = reference.id != "default"

    m = _unfilled_key_class(ElasticModel, **config).elastic_create(data)

    assert m.model_extra == reference.model_extra
    assert m.elastic_is_loaded("id") == filled
    if filled:
        assert m.id == reference.id
    else:
        with pytest.raises(NotLoadedFieldError):
            m.id


@pytest.mark.parametrize("config, data", UNFILLED_KEY_CASES)
def test_nested_basemodel_unfilled_key_lands_in_model_extra_as_in_pydantic(config, data):
    """
    Те саме правило для вкладеного `BaseModel`, який при `validate=False` будує бібліотека, а не pydantic.
    """
    Inner = _unfilled_key_class(BaseModel, **config)
    reference = Inner.model_validate(data)
    filled = reference.id != "default"

    class Outer(ElasticModel):
        inner: Inner

    m = Outer.elastic_create({"inner": data}, validate=False)

    assert m.inner.model_extra == reference.model_extra
    assert ("id" in m.inner.model_fields_set) == filled
    if filled:
        assert m.inner.id == reference.id


# --------------------------
# Відповідність BaseModel: валідація значення за правилами поля
# --------------------------

T = TypeVar("T")


class _Color(Enum):
    RED = "red"


class _Point:
    """Довільний клас, для якого pydantic не має схеми."""


POINT = _Point()


def _field_constraint(base: type[BaseModel]):
    class Rule(base):
        x: int = Field(gt=0)

    return Rule, {"x": -1}


def _strict_config(base: type[BaseModel]):
    class Rule(base):
        model_config = ConfigDict(strict=True)
        x: int

    return Rule, {"x": "5"}


def _strip_whitespace(base: type[BaseModel]):
    class Rule(base):
        model_config = ConfigDict(str_strip_whitespace=True)
        x: str

    return Rule, {"x": "  a  "}


def _to_lower(base: type[BaseModel]):
    class Rule(base):
        model_config = ConfigDict(str_to_lower=True)
        x: str

    return Rule, {"x": "ABC"}


def _numbers_to_str(base: type[BaseModel]):
    class Rule(base):
        model_config = ConfigDict(coerce_numbers_to_str=True)
        x: str

    return Rule, {"x": 5}


def _enum_values(base: type[BaseModel]):
    class Rule(base):
        model_config = ConfigDict(use_enum_values=True)
        x: _Color

    return Rule, {"x": "red"}


def _arbitrary_type(base: type[BaseModel]):
    class Rule(base):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        x: _Point

    return Rule, {"x": POINT}


def _generic_parameter(base: type[BaseModel]):
    class Box(base, Generic[T]):
        x: T

    return Box[int], {"x": "5"}


def _validated_default(base: type[BaseModel]):
    class Rule(base):
        x: int = Field("5", validate_default=True)

    return Rule, {}


FIELD_RULE_CASES = [
    pytest.param(_field_constraint, id="field-constraint"),
    pytest.param(_strict_config, id="strict-config"),
    pytest.param(_strip_whitespace, id="str_strip_whitespace"),
    pytest.param(_to_lower, id="str_to_lower"),
    pytest.param(_numbers_to_str, id="coerce_numbers_to_str"),
    pytest.param(_enum_values, id="use_enum_values"),
    pytest.param(_arbitrary_type, id="arbitrary_types_allowed"),
    pytest.param(_generic_parameter, id="generic"),
    pytest.param(_validated_default, id="validate_default"),
]


def _validation_outcome(create) -> tuple[str, Any]:
    """
    Результат побудови моделі в порівнюваному вигляді: `model_dump()` при успіху або типи помилок при `ValidationError`.
    """
    try:
        return "ok", create().model_dump()
    except ValidationError as exc:
        return "error", [error["type"] for error in exc.errors()]


@pytest.mark.parametrize("build", FIELD_RULE_CASES)
def test_elastic_create_validates_values_by_field_rules_as_pydantic(build):
    """
    `elastic_create(validate=True)` валідує значення за правилами поля pydantic: обмеженнями `Field`, конфігом моделі, параметром generic і `validate_default`.
    Для того самого класу на `BaseModel` і тих самих даних результат `model_dump()` чи типи помилок збігаються з `model_validate`.
    `defaults=True`, бо інакше дефолт не потрапляє в модель і `validate_default` нічого перевірити.
    """
    reference_cls, data = build(BaseModel)
    elastic_cls, _ = build(ElasticModel)

    expected = _validation_outcome(lambda: reference_cls.model_validate(data))

    assert _validation_outcome(lambda: elastic_cls.elastic_create(data, defaults=True)) == expected


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="Python 3.10 не розв'язує рядок усередині вбудованого generic (`list['Leaf']`); 3.10 не підтримується офіційно, обмеження описане в README",
)
def test_local_classes_with_string_annotation_match_pydantic():
    """
    Класи, оголошені всередині функції, де один посилається на другий рядком, `elastic_create` будує так само, як `model_validate`.
    pydantic розв'язує такий рядок під час оголошення класу, а пізніше локального імені вже ніде не видно.
    """
    def build(base: type[BaseModel]) -> type[BaseModel]:
        class Leaf(base):
            v: int

        class Root(base):
            leaf: "Leaf"
            leaves: list["Leaf"]

        return Root

    data = {"leaf": {"v": "1"}, "leaves": [{"v": "2"}]}

    assert build(ElasticModel).elastic_create(data).model_dump() == build(BaseModel).model_validate(data).model_dump()


# --------------------------
# Валідатори класу і рекурсія у вкладених моделях
# --------------------------

def _leaf_with_validators(base: type[BaseModel], calls: list[str]) -> type[BaseModel]:
    """
    Модель з вкладеною моделлю, що має валідатори класу всіх видів; кожен валідатор записує свій виклик у `calls`.
    """
    class Leaf(base):
        a: int
        b: int

        @field_validator("a")
        @classmethod
        def on_field(cls, value):
            calls.append("field")
            return value

        @model_validator(mode="before")
        @classmethod
        def on_before(cls, value):
            calls.append("before")
            return value

        @model_validator(mode="wrap")
        @classmethod
        def on_wrap(cls, value, handler):
            calls.append("wrap")
            return handler(value)

        @model_validator(mode="after")
        def on_after(self):
            calls.append("after")
            return self

    class Root(base):
        leaf: Leaf

    return Root


def test_nested_model_validators_in_elastic_create_match_pydantic_for_instances():
    """
    `elastic_create` будує вкладену модель сам, тому її `field`- і `before`-валідатори не спрацьовують.
    Готовий частковий екземпляр далі проходить схему поля, і pydantic запускає на ньому `wrap`- і `after`-валідатори.
    Те саме pydantic робить з будь-яким готовим екземпляром у полі, тож еталон — `model_validate` з екземпляром з `model_construct`.
    На звичайному шляху спрацьовують ті самі валідатори, що й у того самого класу на `BaseModel`.
    """
    calls: list[str] = []
    root_cls = _leaf_with_validators(ElasticModel, calls)

    reference_calls: list[str] = []
    reference_cls = _leaf_with_validators(BaseModel, reference_calls)
    leaf_cls = reference_cls.model_fields["leaf"].annotation

    root_cls.elastic_create({"leaf": {"a": "1"}})
    reference_cls.model_validate({"leaf": leaf_cls.model_construct(a=1)})
    assert calls == reference_calls

    calls.clear()
    reference_calls.clear()
    full = {"leaf": {"a": 1, "b": 2}}
    root_cls.model_validate(full)
    reference_cls.model_validate(full)
    assert calls == reference_calls


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="Python 3.10 не розв'язує рядок усередині вбудованого generic (`list['Node']`); 3.10 не підтримується офіційно, обмеження описане в README",
)
def test_recursive_model_builds_partial_children():
    """
    Рекурсивна модель будує часткових нащадків на кожному рівні.
    """
    class Node(ElasticModel):
        name: str
        tag: str
        children: list["Node"] = []

    m = Node.elastic_create({"name": "root", "children": [{"name": "kid", "children": [{"tag": "t"}]}]})

    assert m.children[0].elastic_loaded_fields == {"name", "children"}
    assert m.children[0].children[0].elastic_loaded_fields == {"tag"}


# --------------------------
# Невалідні значення: відмова як у pydantic
# --------------------------

CONTAINER_MISMATCH_CASES = [
    pytest.param(list[int], 5, id="list<-int"),
    pytest.param(list[int], "abc", id="list<-str"),
    pytest.param(tuple[int, int], 5, id="tuple<-int"),
    pytest.param(tuple[int, int], {1, 2}, id="tuple<-set"),
    pytest.param(dict[str, int], 5, id="dict<-int"),
    pytest.param(set[int], 5, id="set<-int"),
]


@pytest.mark.parametrize("annotation, value", CONTAINER_MISMATCH_CASES)
def test_container_field_with_value_of_other_type_matches_pydantic(annotation, value):
    """
    Значення не того типу в контейнерному полі `elastic_create` не обходить як контейнер, а віддає pydantic як є.
    Тому результат той самий, що в `model_validate`: `ValidationError` з тими самими типами помилок або та сама модель, якщо pydantic таке значення приймає.
    """
    reference_cls = create_model("Holder", __base__=BaseModel, x=(annotation, ...))
    elastic_cls = create_model("Holder", __base__=ElasticModel, x=(annotation, ...))
    data = {"x": value}

    expected = _validation_outcome(lambda: reference_cls.model_validate(data))

    assert _validation_outcome(lambda: elastic_cls.elastic_create(data)) == expected


@pytest.mark.parametrize("inner_data", [{"a": 1}, {"a": "1"}], ids=["valid", "coercible"])
def test_strict_validate_without_validate_reaches_nested_basemodel(inner_data):
    """
    `strict_validate=True` вмикає валідацію і при `validate=False`, зокрема у вкладеному `BaseModel`.
    Результат той самий, що в `model_validate(..., strict=True)` того самого класу на `BaseModel`.
    """
    class Inner(BaseModel):
        a: int

    reference_cls = create_model("Holder", __base__=BaseModel, inner=(Inner, ...))
    elastic_cls = create_model("Holder", __base__=ElasticModel, inner=(Inner, ...))
    data = {"inner": inner_data}

    expected = _validation_outcome(lambda: reference_cls.model_validate(data, strict=True))

    assert _validation_outcome(lambda: elastic_cls.elastic_create(data, validate=False, strict_validate=True)) == expected


@pytest.mark.parametrize("extra", ["ignore", "allow"])
@pytest.mark.parametrize("key", ["cls", "_fields_set"])
def test_data_key_named_like_model_construct_parameter_matches_pydantic(key, extra):
    """
    `elastic_create` збирає модель через `model_construct(cls, _fields_set=None, **values)`, тож ключі `cls` і `_fields_set` збігаються з його параметрами.
    Для моделі це звичайні невідомі ключі: результат той самий, що в `model_validate`, а сам ключ лежить в `elastic_extra`.
    """
    def build(base: type[BaseModel]) -> type[BaseModel]:
        class Holder(base):
            model_config = ConfigDict(extra=extra)
            a: int

        return Holder

    data = {"a": 1, key: {"zzz"}}
    reference = build(BaseModel).model_validate(data)

    m = build(ElasticModel).elastic_create(data)

    assert m.model_dump() == reference.model_dump()
    assert m.model_extra == reference.model_extra
    assert m.elastic_loaded_fields == reference.model_fields_set
    assert m.elastic_extra == {key: {"zzz"}}


# --------------------------
# Поля Optional/Union в elastic_create (FR-16)
# --------------------------

def _describe(value: Any) -> Any:
    """
    Значення поля в порівнюваному вигляді: модель — її клас, завантажені поля (рекурсивно), `model_fields_set` і ключі поза полями; контейнери — поелементно.
    Часткову модель через `model_dump()` не порівняти, бо незавантажених полів у ній немає.
    """
    if isinstance(value, BaseModel):
        unfilled = value.elastic_extra if isinstance(value, ElasticModel) else value.model_extra
        return type(value), {name: _describe(item) for name, item in vars(value).items()}, value.model_fields_set, unfilled
    if isinstance(value, list):
        return [_describe(item) for item in value]
    if isinstance(value, dict):
        return {key: _describe(item) for key, item in value.items()}
    return value


def _field_outcome(holder_cls: type[ElasticModel], field: str, value: Any, **options: Any) -> tuple[str, Any]:
    """
    Результат `elastic_create` для одного поля: `_describe` значення при успіху або `(type, loc)` помилок при `ValidationError`.
    """
    try:
        return "ok", _describe(getattr(holder_cls.elastic_create({field: value}, **options), field))
    except ValidationError as exc:
        return "error", [(error["type"], error["loc"]) for error in exc.errors()]


def _pydantic_outcome(create) -> tuple[str, Any]:
    """
    Результат звичайного шляху pydantic: `model_dump()` при успіху або `(type, loc)` помилок при `ValidationError`.
    """
    try:
        return "ok", create().model_dump()
    except ValidationError as exc:
        return "error", [(error["type"], error["loc"]) for error in exc.errors()]


def _payment_models(base: type[BaseModel]) -> type[ElasticModel]:
    """
    Власник із полем дискримінованого `Union[Card, Bank]` і прямим полем `Card`; гілки — на `base`.
    Пряме поле — еталон: гілка `Union` з тими самими даними й параметрами має дорівнювати йому.
    """
    class Card(base):
        kind: Literal["card"]
        pan: str
        holder: str
        limit: int = 100

    class Bank(base):
        kind: Literal["bank"]
        iban: str

    class Holder(ElasticModel):
        method: Annotated[Union[Card, Bank], Field(discriminator="kind")]
        card: Card

    return Holder


def _union_holder(base: type[BaseModel], fields: tuple[str, ...] = ("tagged", "plain")) -> type[BaseModel]:
    """Клас на `base` з обов'язковими полями `fields`: `tagged` — дискримінований `Union[Card, Bank]`, `plain` — звичайний з тими самими гілками."""
    class Card(base):
        kind: Literal["card"]
        pan: str
        holder: str

    class Bank(base):
        kind: Literal["bank"]
        iban: str

    annotations = {"tagged": Annotated[Union[Card, Bank], Field(discriminator="kind")], "plain": Union[Card, Bank]}
    return create_model("Holder", __base__=base, **{name: (annotations[name], ...) for name in fields})


UNION_VALUES = [
    pytest.param({"kind": "card", "pan": "1", "holder": "X"}, id="full-card"),
    pytest.param({"kind": "bank", "iban": "UA"}, id="full-bank"),
    pytest.param({"kind": "card", "holder": "X"}, id="partial-card"),
    pytest.param({"pan": "1", "holder": "X"}, id="no-tag"),
    pytest.param({"kind": "zzz", "iban": "UA"}, id="unknown-tag"),
]


@pytest.mark.parametrize("value", UNION_VALUES)
def test_union_fields_regular_path_matches_pydantic(value):
    """
    U.10: обхід `Union` живе лише в `elastic_create`, тож `model_validate` і конструктор дають те саме, що клас на `BaseModel`.
    Порівнюються результат і помилки разом із `loc`, де є мітки гілок.
    """
    reference_cls = _union_holder(BaseModel)
    elastic_cls = _union_holder(ElasticModel)
    data = {"tagged": value, "plain": value}

    assert _pydantic_outcome(lambda: elastic_cls.model_validate(data)) == _pydantic_outcome(lambda: reference_cls.model_validate(data))
    assert _pydantic_outcome(lambda: elastic_cls(**data)) == _pydantic_outcome(lambda: reference_cls(**data))


@pytest.mark.parametrize("validate", [True, False])
@pytest.mark.parametrize("base", [ElasticModel, BaseModel])
def test_discriminated_union_partial_branch_matches_direct_field(base, validate):
    """
    U.1, issue #1 і #2: частковий `dict` у полі дискримінованого `Union` дає те саме, що пряме поле класу гілки, — часткову модель.
    `BaseModel` з валідацією pydantic частковим не приймає: і гілку, і пряме поле перевіряє адаптер поля, тож помилка — та сама, що в `model_validate`.
    """
    holder = _payment_models(base)
    value = {"kind": "card", "holder": "X"}

    if base is BaseModel and validate:
        with pytest.raises(ValidationError) as caught:
            holder.model_validate({"method": value, "card": value})
        expected = [(error["type"], error["loc"][1:]) for error in caught.value.errors() if error["loc"][0] == "method"]

        assert _field_outcome(holder, "method", value) == ("error", expected)
        return

    assert _field_outcome(holder, "method", value, validate=validate) == _field_outcome(holder, "card", value, validate=validate)


def _optional_holder() -> type[ElasticModel]:
    """
    Власник з полями `Optional[X]` і прямими полями `X` для тих самих `X`.
    Дискримінований `Union` усередині `Optional` записаний у трьох формах, остання — через `|`.
    """
    class Card(ElasticModel):
        kind: Literal["card"]
        pan: str
        holder: str

    class Bank(ElasticModel):
        kind: Literal["bank"]
        iban: str

    Tagged = Annotated[Union[Card, Bank], Field(discriminator="kind")]

    class Holder(ElasticModel):
        card: Card
        cards: list[Card]
        card_map: dict[str, Card]
        method: Tagged
        opt_card: Optional[Card]
        opt_cards: Optional[list[Card]]
        opt_card_map: Optional[dict[str, Card]]
        opt_outer: Optional[Tagged]
        opt_inner: Annotated[Optional[Union[Card, Bank]], Field(discriminator="kind")]
        opt_pipe: Card | Bank | None = Field(discriminator="kind")

    return Holder


_PARTIAL_CARD = {"kind": "card", "holder": "X"}


@pytest.mark.parametrize("validate", [True, False])
@pytest.mark.parametrize("optional_field, direct_field, value", [
    pytest.param("opt_card", "card", _PARTIAL_CARD, id="Optional[Card]"),
    pytest.param("opt_cards", "cards", [_PARTIAL_CARD], id="Optional[list[Card]]"),
    pytest.param("opt_card_map", "card_map", {"a": _PARTIAL_CARD}, id="Optional[dict[str, Card]]"),
    pytest.param("opt_outer", "method", _PARTIAL_CARD, id="Optional[Annotated[U, disc]]"),
    pytest.param("opt_inner", "method", _PARTIAL_CARD, id="Annotated[Optional[U], disc]"),
    pytest.param("opt_pipe", "method", _PARTIAL_CARD, id="Card|Bank|None=Field(disc)"),
])
def test_optional_field_behaves_as_inner_type(optional_field, direct_field, value, validate):
    """
    U.8: `Optional[X]` поводиться як `X` — часткові дані дають те саме, що пряме поле `X`.
    """
    holder = _optional_holder()

    expected = _field_outcome(holder, direct_field, value, validate=validate)

    assert expected[0] == "ok"
    assert _field_outcome(holder, optional_field, value, validate=validate) == expected


@pytest.mark.parametrize("validate", [True, False])
@pytest.mark.parametrize("field", ["opt_card", "opt_cards", "opt_card_map", "opt_outer", "opt_inner", "opt_pipe"])
def test_optional_field_keeps_none(field, validate):
    """U.8: `None` у полі `Optional` лишається `None`."""
    assert getattr(_optional_holder().elastic_create({field: None}, validate=validate), field) is None


# `str`-Enum: pydantic зіставляє рядок-тег з членом, бо член дорівнює своєму рядку.
# Для звичайного `Enum` рядок `"card"` pydantic відкидає з `union_tag_invalid`.
class _Kind(str, Enum):
    CARD = "card"
    BANK = "bank"


def _kind_tag(value: Any) -> str | None:
    """Callable-дискримінатор: тег `Tag` за полем `kind` словника чи екземпляра."""
    kind = value.get("kind") if isinstance(value, dict) else getattr(value, "kind", None)
    return {"card": "c", "bank": "b"}.get(kind)


def _string_tag_form(base):
    class Card(base):
        kind: Literal["card"]
        pan: str
        holder: str

    class Bank(base):
        kind: Literal["bank"]
        iban: str

    class Holder(base):
        method: Annotated[Union[Card, Bank], Field(discriminator="kind")]

    return Holder, {"kind": "card", "holder": "X"}, "Card", {"kind", "holder"}


def _callable_tag_form(base):
    class Card(base):
        kind: Literal["card"]
        pan: str
        holder: str

    class Bank(base):
        kind: Literal["bank"]
        iban: str

    class Holder(base):
        method: Annotated[Union[Annotated[Card, Tag("c")], Annotated[Bank, Tag("b")]], Discriminator(_kind_tag)]

    return Holder, {"kind": "card", "holder": "X"}, "Card", {"kind", "holder"}


def _alias_tag_form(base):
    class Card(base):
        kind: Literal["card"] = Field(alias="_kind")
        pan: str
        holder: str

    class Bank(base):
        kind: Literal["bank"] = Field(alias="_kind")
        iban: str

    class Holder(base):
        method: Annotated[Union[Card, Bank], Field(discriminator="kind")]

    return Holder, {"_kind": "card", "holder": "X"}, "Card", {"kind", "holder"}


def _enum_tag_form(base):
    class Card(base):
        kind: Literal[_Kind.CARD]
        pan: str
        holder: str

    class Bank(base):
        kind: Literal[_Kind.BANK]
        iban: str

    class Holder(base):
        method: Annotated[Union[Card, Bank], Field(discriminator="kind")]

    return Holder, {"kind": "card", "holder": "X"}, "Card", {"kind", "holder"}


def _recursive_tag_form(base):
    # Рекурсивна гілка потрапляє в таблицю тегів як `definition-ref` на `definitions` схеми
    class File(base):
        kind: Literal["file"]
        name: str

    class Folder(base):
        kind: Literal["folder"]
        name: str
        children: List[Annotated[Union["Folder", File], Field(discriminator="kind")]]

    class Holder(base):
        method: Annotated[Union[Folder, File], Field(discriminator="kind")]

    return Holder, {"kind": "folder", "children": [{"kind": "file"}]}, "Folder", {"kind", "children"}


TAG_FORMS = [
    pytest.param(_string_tag_form, id="string"),
    pytest.param(_callable_tag_form, id="callable"),
    pytest.param(_alias_tag_form, id="alias"),
    pytest.param(_enum_tag_form, id="enum"),
    pytest.param(_recursive_tag_form, id="recursive"),
]


@pytest.mark.parametrize("validate", [True, False])
@pytest.mark.parametrize("form", TAG_FORMS)
def test_discriminated_union_builds_branch_of_tag(form, validate):
    """
    U.2: гілку обирає тег із даних за таблицею pydantic — для кожної форми дискримінатора.
    """
    holder, value, branch_name, loaded = form(ElasticModel)

    result = holder.elastic_create({"method": value}, validate=validate).method

    assert type(result).__name__ == branch_name
    assert result.elastic_loaded_fields == loaded


@pytest.mark.parametrize("tag", [None, "zzz"], ids=["no-tag", "unknown-tag"])
@pytest.mark.parametrize("form", TAG_FORMS)
def test_discriminated_union_without_known_tag(form, tag):
    """
    U.2: без тегу чи з невідомим тегом гілки немає.
    З валідацією помилка того самого типу, що `model_validate` того самого класу на `BaseModel`; без валідації значення лишається `dict`.
    """
    reference_cls, value, _, _ = form(BaseModel)
    elastic_cls, _, _, _ = form(ElasticModel)
    tag_key = next(iter(value))
    value = {key: item for key, item in value.items() if key != tag_key}
    if tag is not None:
        value[tag_key] = tag
    data = {"method": value}

    expected = _validation_outcome(lambda: reference_cls.model_validate(data))

    assert expected[0] == "error"
    assert _validation_outcome(lambda: elastic_cls.elastic_create(data)) == expected
    assert elastic_cls.elastic_create(data, validate=False).method == value


def _raised(create) -> tuple[type, str] | None:
    """Виняток виклику як `(тип, текст)`; `None`, якщо виклик пройшов."""
    try:
        create()
    except Exception as exc:
        return type(exc), str(exc)
    return None


def test_discriminated_union_does_not_run_other_branch_code():
    """
    U.3: код необраної гілки не виконується — `Bank.model_post_init` не викликається.
    Обрана гілка з after-валідатором поводиться як пряме поле: без валідації — часткова модель,
    з валідацією pydantic запускає валідатор на готовому екземплярі, і той падає на незавантаженому `pan`.
    """
    calls: list[str] = []

    class Card(ElasticModel):
        kind: Literal["card"]
        pan: str
        holder: str

        @model_validator(mode="after")
        def pan_is_digits(self):
            if not self.pan.isdigit():
                raise ValueError("pan must be digits")
            return self

    class Bank(ElasticModel):
        kind: Literal["bank"]
        iban: str

        def model_post_init(self, context: Any) -> None:
            calls.append("Bank.model_post_init")

    class Holder(ElasticModel):
        method: Annotated[Union[Card, Bank], Field(discriminator="kind")]
        card: Card

    value = {"kind": "card", "holder": "X"}

    assert _field_outcome(Holder, "method", value, validate=False) == _field_outcome(Holder, "card", value, validate=False)

    expected = _raised(lambda: Holder.elastic_create({"card": value}))
    assert expected is not None
    assert _raised(lambda: Holder.elastic_create({"method": value})) == expected

    assert calls == []


@pytest.mark.parametrize("options, value, status", [
    pytest.param({"defaults": True}, {"kind": "card", "holder": "X"}, "ok", id="defaults"),
    pytest.param({"strict_validate": True}, {"kind": "card", "limit": "5"}, "error", id="strict"),
    pytest.param({"strict_validate": True, "validate": False}, {"kind": "card", "limit": "5"}, "error", id="strict-without-validate"),
])
def test_create_options_reach_union_branch(options, value, status):
    """
    U.9: `defaults` і `strict_validate` доходять до гілки `Union` так само, як до прямого поля.
    """
    holder = _payment_models(ElasticModel)

    expected = _field_outcome(holder, "card", value, **options)

    assert expected[0] == status
    assert _field_outcome(holder, "method", value, **options) == expected


@pytest.mark.parametrize("validate", [True, False])
def test_list_of_discriminated_union_builds_each_element_by_tag(validate):
    """
    U.9: у `list[дискримінований Union]` кожен елемент будується гілкою свого тегу.
    """
    class Card(ElasticModel):
        kind: Literal["card"]
        pan: str
        holder: str

    class Bank(ElasticModel):
        kind: Literal["bank"]
        iban: str

    class Holder(ElasticModel):
        methods: list[Annotated[Union[Card, Bank], Field(discriminator="kind")]]

    m = Holder.elastic_create({"methods": [{"kind": "card", "holder": "X"}, {"kind": "bank"}]}, validate=validate)

    assert [type(item) for item in m.methods] == [Card, Bank]
    assert [item.elastic_loaded_fields for item in m.methods] == [{"kind", "holder"}, {"kind"}]


# Гілки звичайного `Union` для тестів правила вибору (U.4-U.7).
class _UCard(ElasticModel):
    kind: Literal["card"]
    pan: str
    holder: str


class _UBank(ElasticModel):
    kind: Literal["bank"]
    iban: str


class _UCardBM(BaseModel):
    kind: Literal["card"]
    pan: str
    holder: str


class _UBankBM(BaseModel):
    kind: Literal["bank"]
    iban: str


class _UA(ElasticModel):
    x: int
    a: str


class _UB(ElasticModel):
    x: int
    b: str


class _ULong(ElasticModel):
    name: str
    a: int
    b: int
    c: int


class _UShort(ElasticModel):
    name: str


def _holder_of(annotation: Any, base: type[BaseModel] = ElasticModel, **config: Any) -> type[BaseModel]:
    """
    Клас на `base` з одним обов'язковим полем `value` анотації `annotation` і конфігом `config`.
    Конфіг задає проміжний клас, бо pydantic 2.11.0 не приймає в `create_model` `__base__` разом з `__config__`.
    """
    class Configured(base):
        model_config = ConfigDict(**config)

    return create_model("Holder", __base__=Configured, value=(annotation, ...))


@pytest.mark.parametrize("annotation, value, validates, expected", [
    pytest.param(Union[_UCard, _UBank], {"kind": "bank"}, [True, False], _UBank, id="tag-value-picks-bank"),
    pytest.param(_UCard | _UBank, {"kind": "bank"}, [True, False], _UBank, id="pipe-form"),
    pytest.param(Union[_UCardBM, _UBankBM], {"kind": "bank", "holder": "X"}, [False], _UBankBM, id="basemodel-without-validation"),
    pytest.param(Union[_UA, _UB], {"x": 1}, [True, False], _UA, id="equal-fields-left"),
    pytest.param(Union[_UA, _UB], {"x": 1, "b": "q"}, [True, False], _UB, id="more-fields-wins"),
    pytest.param(Union[_ULong, _UShort], {"name": "n", "a": 1, "b": 2}, [True, False], _UShort, id="complete-beats-more-fields"),
])
def test_plain_union_picks_branch_for_partial_data(annotation, value, validates, expected):
    """
    U.4: на часткових даних звичайний `Union` обирає гілку за правилом FR-16 за будь-якого `validate`:
    гілка з відкинутими значеннями вибуває; далі повні, далі більше заповнених полів, далі ліва.
    pydantic таких даних не приймає, тож очікуваний клас вписаний явно.
    """
    holder = _holder_of(annotation)

    for validate in validates:
        assert type(holder.elastic_create({"value": value}, validate=validate).value) is expected


def test_plain_union_without_validation_builds_first_branch_when_every_branch_rejects():
    """
    U.4: без валідації, коли значення відкидає кожна гілка, будується перша за порядком гілка зі значеннями як є.
    `_UCard` заповнює два поля, `_UBank` — одне, тож перша — `_UCard`.
    """
    result = _holder_of(Union[_UCard, _UBank]).elastic_create({"value": {"kind": "x", "pan": 1}}, validate=False).value

    assert type(result) is _UCard
    assert (result.kind, result.pan) == ("x", 1)


class _TypedCard(TypedDict):
    kind: str


class _Opaque:
    """Довільний тип без схеми pydantic: адаптер для нього будується лише з `arbitrary_types_allowed`."""


def _union_parity_models(base: type[BaseModel]):
    class Long(base):
        name: str
        note: str
        more: str

    class Short(base):
        name: str
        note: str

    class ForbidA(base):
        model_config = ConfigDict(extra="forbid")
        x: int
        y: int

    class PlainX(base):
        x: int

    class Card(base):
        kind: Literal["card"]
        pan: str
        holder: str

    return {
        "long-short": (Union[Long, Short], {"name": "n", "note": "t"}),
        "forbid": (Union[ForbidA, PlainX], {"x": 1, "y": 2, "z": 3}),
        "dict-card": (Union[dict, Card], {"kind": "card", "pan": "1", "holder": "X"}),
        "card-dict": (Union[Card, dict], {"kind": "card", "pan": "1", "holder": "X"}),
        "str-card": (Union[str, Card], {"kind": "card", "pan": "1", "holder": "X"}),
        "typeddict-card": (Union[_TypedCard, Card], {"kind": "card", "holder": "X"}),
    }


@pytest.mark.parametrize("case", ["long-short", "forbid", "dict-card", "card-dict", "str-card", "typeddict-card"])
def test_plain_union_matches_pydantic_on_accepted_data(case):
    """
    U.5: на даних, які pydantic приймає, клас результату з валідацією той самий, що в `model_validate` того самого класу на `BaseModel`.
    Гілку-не-модель, що приймає `dict` (`dict`, `TypedDict`), вирішує pydantic.
    """
    reference_annotation, value = _union_parity_models(BaseModel)[case]
    annotation, _ = _union_parity_models(ElasticModel)[case]

    expected = type(_holder_of(reference_annotation, BaseModel).model_validate({"value": value}).value).__name__

    assert type(_holder_of(annotation).elastic_create({"value": value}).value).__name__ == expected


@pytest.mark.parametrize("annotation, expected", [
    pytest.param(Union[_UCard, dict], dict, id="card-dict"),
    pytest.param(Union[str, _UCard], _UCard, id="str-card"),
    pytest.param(Union[_TypedCard, _UCard], dict, id="typeddict-card"),
])
def test_plain_union_with_non_model_branch_without_validation(annotation, expected):
    """
    U.5: без валідації гілка-не-модель, що приймає `dict`, лишає значення `dict`; гілка, що не приймає, не заважає будувати модель.
    """
    result = _holder_of(annotation).elastic_create({"value": {"kind": "card", "holder": "X"}}, validate=False).value

    assert type(result) is expected


@pytest.mark.parametrize("validate", [True, False])
def test_plain_union_uses_owner_config_for_non_model_branches(validate):
    """
    U.5: адаптер гілки-не-моделі будується з конфігом власника поля: без `arbitrary_types_allowed` схему `_Opaque` pydantic не будує.
    """
    holder = _holder_of(Union[_Opaque, _UCard], arbitrary_types_allowed=True)

    result = holder.elastic_create({"value": {"kind": "card", "holder": "X"}}, validate=validate).value

    assert type(result) is _UCard
    assert result.elastic_loaded_fields == {"kind", "holder"}


def _int_str_models(base: type[BaseModel]):
    class IntX(base):
        x: int

    class StrX(base):
        x: str

    return Union[IntX, StrX]


def _validated_pair_models(base: type[BaseModel]):
    class VA(base):
        x: int
        y: int

        @model_validator(mode="after")
        def x_not_greater(self):
            if self.x > self.y:
                raise ValueError("x > y")
            return self

    class VB(base):
        x: int
        y: int

    return Union[VA, VB]


def test_plain_union_documented_extensions():
    """
    U.6: розширення FR-16, описані в README.
    - Точність збігу не враховується: `{"x": "1"}` у `Union[IntX, StrX]` дає `IntX`, а pydantic — `StrX`.
    - Валідатори класу гілки у виборі не беруть участі: `Union[VA, VB]` обирає `VA`, і його after-валідатор дає помилку, а pydantic — `VB`.
      Адаптер поля отримує вже екземпляр `VA`, тож гілка `VB` теж його відкидає (`model_type`).
    Зміна поведінки ламає тест, тож README треба оновити разом з нею.
    """
    data = {"value": {"x": "1"}}
    assert type(_holder_of(_int_str_models(BaseModel), BaseModel).model_validate(data).value).__name__ == "StrX"
    assert type(_holder_of(_int_str_models(ElasticModel)).elastic_create(data).value).__name__ == "IntX"

    data = {"value": {"x": 5, "y": 1}}
    assert type(_holder_of(_validated_pair_models(BaseModel), BaseModel).model_validate(data).value).__name__ == "VB"
    with pytest.raises(ValidationError) as caught:
        _holder_of(_validated_pair_models(ElasticModel)).elastic_create(data)
    assert [error["type"] for error in caught.value.errors()] == ["value_error", "model_type"]


def test_union_error_when_no_branch_fits_matches_first_branch_direct_field():
    """
    U.7: коли значення не приймає жодна гілка, помилка — як у прямого поля першої за порядком гілки, без `missing` відсутніх полів.
    """
    class Holder(ElasticModel):
        card: _UCard
        plain: Union[_UCard, _UBank]
        optional: Optional[_UCard]

    value = {"kind": "card", "holder": 5}

    expected = _field_outcome(Holder, "card", value)

    assert expected == ("error", [("string_type", ())])
    assert _field_outcome(Holder, "plain", value) == expected
    assert _field_outcome(Holder, "optional", value) == expected


class _UY(ElasticModel):
    y: int


def test_union_branch_without_filled_fields_does_not_accept_data():
    """
    Гілка, якій дані не заповнюють жодного поля, значень не підтверджує.
    `{"x": "bad"}` у `Union[_UA, _UY]` дає помилку прямого поля `_UA`, а не порожній `_UY` з `"x"` в `elastic_extra` (знахідка рев'ю 2026-10-24).
    """
    class Holder(ElasticModel):
        direct: _UA
        plain: Union[_UA, _UY]

    value = {"x": "bad"}

    expected = _field_outcome(Holder, "direct", value)

    assert expected == ("error", [("int_parsing", ())])
    assert _field_outcome(Holder, "plain", value) == expected


@pytest.mark.parametrize("value", [{"x": 5, "y": 1}, {"x": 1}], ids=["validator-rejects-first", "partial"])
def test_plain_union_of_basemodel_branches_with_validation_matches_pydantic(value):
    """
    Гілки-`BaseModel` з валідацією будує й перевіряє адаптер поля pydantic, як пряме поле, тож результат і помилки — як у `model_validate`.
    Розширення правила вибору тут нічого не додає: частковий `BaseModel` pydantic однаково не прийме (знахідка рев'ю 2026-10-24).
    """
    annotation = _validated_pair_models(BaseModel)
    data = {"value": value}

    def outcome(create):
        # Клас гілки порівнюється окремо: `model_dump()` у `VA` і `VB` однаковий
        try:
            return "ok", type(create().value).__name__
        except ValidationError as exc:
            return "error", [(error["type"], error["loc"][-2:]) for error in exc.errors()]

    expected = outcome(lambda: _holder_of(annotation, BaseModel).model_validate(data))

    assert outcome(lambda: _holder_of(annotation).elastic_create(data)) == expected


@pytest.mark.parametrize("field", ["tagged", "plain"])
def test_validation_methods_with_partial_union_branch(field):
    """
    U.11: шляхи `elastic_is_valid()` для часткової гілки `Union` дорівнюють `loc` помилок `model_validate` того самого класу на `BaseModel`.
    Після присвоєння відсутнього поля модель валідна, а копія з перевірки зберігає гілку та її `elastic_extra`.
    """
    data = {field: {"kind": "card", "holder": "X", "zz": 1}}

    with pytest.raises(ValidationError) as caught:
        _union_holder(BaseModel, (field,)).model_validate(data)
    expected_paths = [".".join(str(part) for part in error["loc"]) for error in caught.value.errors()]

    m = _union_holder(ElasticModel, (field,)).elastic_create(data)

    assert m.elastic_is_valid() == (False, expected_paths)

    getattr(m, field).pan = "1"
    assert m.elastic_is_valid() == (True, [])

    validated = m.elastic_get_validated_model()

    assert type(getattr(validated, field)).__name__ == "Card"
    assert getattr(validated, field).elastic_extra == {"zz": 1}


# --------------------------
# Публічний контракт: README як джерело підпису конструктора
# --------------------------

def _documented_param_names(params_text: str) -> set[str]:
    """
    Імена параметрів із тексту підпису, як його записано в README (вміст дужок).

    Текст підставляється у фіктивне `def` і розбирається через `ast`, тому коми всередині анотацій (`dict[str, Any]`) не ламають розбір.
    Маркер keyword-only (`*`) теж розбирається коректно й імен не додає.
    """
    import ast

    function = ast.parse(f"def _documented({params_text}): pass").body[0]
    arguments = function.args
    return {arg.arg for arg in arguments.posonlyargs + arguments.args + arguments.kwonlyargs}


def test_documented_param_names_handles_generic_annotations():
    """
    Коми всередині узагальнених анотацій не мають ламати розбір підпису: `dict[str, Any]` — один параметр, а не два.
    """
    assert _documented_param_names("data: dict[str, Any], validate: bool = True") == {"data", "validate"}


def test_readme_documents_actual_constructor_parameters():
    """
    Підпис конструктора в README звіряється з фактичним підписом коду.

    Тест читає саме README, а не жорстко вписаний перелік імен: інакше він ловив би розходження між кодом
    і тестом, а не між кодом і документацією — тобто пропустив би рівно той дефект, який уже стався.
    """
    import inspect
    import re
    from pathlib import Path

    readme_text = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")

    signature_match = re.search(r"`ElasticModel\.elastic_create\((.*?)\)\s*->", readme_text)
    assert signature_match, "У README немає рядка з підписом `ElasticModel.elastic_create(...) -> ...`"

    documented_params = _documented_param_names(signature_match.group(1))

    actual_params = set(inspect.signature(ElasticModel.elastic_create).parameters)

    assert documented_params == actual_params


    