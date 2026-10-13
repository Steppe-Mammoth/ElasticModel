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
# - updating _elastic_loaded_fields on assignment & deletion / оновлення _elastic_loaded_fields при присвоєнні та видаленні

import pytest
#pytestmark = pytest.mark.filterwarnings("ignore:PydanticSerializationUnexpectedValue")

from typing import Annotated, Any, Dict, List, Literal, Tuple, Union

from pydantic import EmailStr, Field, PrivateAttr, ValidationError, BaseModel, ConfigDict

from gostmodels import ElasticModel


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
    model_fields = u._elastic_get_model_fields()
    assert "debug_flag" not in model_fields


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


def test_discriminated_union_validate_false_keeps_raw():
    """
    Union із дискримінатором: validate=False → лишається «як є» (dict).
    """
    p = Payment.elastic_create({
        "method": {"kind": "bank", "iban": "UA..."}
    }, validate=False)

    assert isinstance(p.method, dict)
    assert p.method["kind"] == "bank"
    assert p.method["iban"] == "UA..."


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
    __setattr__ має позначати поле як «завантажене», а delattr — прибирати з loaded.
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


def test_elastic_extra_always_captures_unknowns_even_with_extra_ignore_and_allow():
    """
    .elastic_extra зберігає unknowns незалежно від налаштування extra.
    """
    class UIgnore(ElasticModel):
        model_config = ConfigDict(extra='ignore')
        a: int

    class UAllow(ElasticModel):
        model_config = ConfigDict(extra='allow')
        a: int

    ui = UIgnore.elastic_create({"a": 1, "u": 2}, validate=False)
    ua = UAllow.elastic_create({"a": 1, "u": 2}, validate=False)

    assert ui.elastic_extra["u"] == 2
    assert ua.elastic_extra["u"] == 2


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
    from gostmodels import NotLoadedFieldError

    # Наявні тести очікують AttributeError на незавантаженому полі, тому базовий клас входить у контракт.
    assert issubclass(NotLoadedFieldError, AttributeError)


def test_unloaded_declared_field_raises_not_loaded_field_error():
    """
    Оголошене поле, якого не було в даних при `defaults=False`, фізично відсутнє в інстансі,
    тому його читання має давати помилку бібліотеки, а не штатну помилку доступу.
    """
    from gostmodels import NotLoadedFieldError

    u = UserSlim.elastic_create({"created": {"at": "t", "by": "sys"}}, validate=False, defaults=False)

    with pytest.raises(NotLoadedFieldError):
        _ = u.email


def test_undeclared_name_keeps_standard_access_behavior():
    """
    Неоголошене ім'я перехоплювач пропускає далі: при `extra='allow'` повертається значення,
    при `extra='ignore'` лишається штатна помилка доступу — і вона не є помилкою бібліотеки.
    """
    from gostmodels import NotLoadedFieldError

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
    from gostmodels import NotLoadedFieldError

    u = UserSlim.elastic_create({"created": {"at": "t", "by": "sys"}}, validate=False, defaults=False)

    with pytest.raises(NotLoadedFieldError) as exc_info:
        _ = u.email
    assert "email" in str(exc_info.value)


def test_interceptor_not_executed_when_reading_loaded_field():
    """
    NFR-2 перевіряється структурно: перехоплювач спрацьовує лише після невдалого звичайного пошуку,
    тому читання завантаженого поля не виконує його коду.
    """
    from gostmodels import NotLoadedFieldError

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
    Перелік має містити лише завантажені дані. Службовий прапорець завершення протікав туди,
    бо `__setattr__` перевіряє його вже після виклику `super()`.
    """
    u = UserSlim.elastic_create({"email": "a@b.com", "created": {"at": "t", "by": "s"}}, validate=False)

    assert [name for name in u.elastic_loaded_fields if name.startswith("_elastic")] == []


def test_loaded_fields_mutation_does_not_change_state():
    """
    Властивість віддає копію, тому зміна отриманого набору не може зробити так, щоб `elastic_is_loaded` брехав.
    """
    u = UserSlim.elastic_create({"email": "a@b.com", "created": {"at": "t", "by": "s"}}, validate=False)
    assert u.elastic_is_loaded("email") is True

    u.elastic_loaded_fields.discard("email")

    assert u.elastic_is_loaded("email") is True


def test_loaded_fields_keep_data_keys_including_underscore_with_extra_allow():
    """
    Фільтр службових імен не зачіпає ключів, що прийшли в даних: усередині конструктора набір наповнює
    цикл по `model_dump()`, а не `__setattr__`. Ключ із підкресленням у даних є завантаженими даними,
    а не приватним атрибутом, тому лишається в переліку.
    """
    class UAllow(ElasticModel):
        model_config = ConfigDict(extra='allow')
        a: int

    u = UAllow.elastic_create({"a": 1, "plain": 2, "_under": 3}, validate=False)

    loaded = u.elastic_loaded_fields
    assert "plain" in loaded
    assert "_under" in loaded


# --------------------------
# Публічний контракт: ключі конфігу для мапінгу аліасів
# --------------------------

@pytest.mark.parametrize(
    "config, alias_is_mapped",
    [
        (ConfigDict(populate_by_name=True), True),
        (ConfigDict(validate_by_name=True), True),
        (ConfigDict(), False),
    ],
    ids=["old-key", "current-key", "no-key"],
)
def test_alias_mapping_reads_both_config_keys(config, alias_is_mapped):
    """
    Мапінг аліасів має працювати і зі старим ключем конфігу, і з актуальним `validate_by_name`.
    Без жодного ключа поведінка лишається колишньою: значення за аліасом не мапиться на поле,
    а потрапляє у сховище невідомих полів, тому доступ за іменем поля падає.
    """
    from gostmodels import NotLoadedFieldError

    class AliasModel(ElasticModel):
        model_config = config
        id: str = Field(alias="_id")

    m = AliasModel.elastic_create({"_id": "x-1"}, validate=False)

    if alias_is_mapped:
        assert m.id == "x-1"
    else:
        assert m.elastic_extra["_id"] == "x-1"
        with pytest.raises(NotLoadedFieldError):
            _ = m.id


# --------------------------
# Публічний контракт: README як джерело підпису конструктора
# --------------------------

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

    # Відкидаємо маркер keyword-only (`*`) і залишаємо лише імена параметрів.
    documented_params = {part.split(":")[0].strip().lstrip("*").strip() for part in signature_match.group(1).split(",")}
    documented_params.discard("")

    actual_params = set(inspect.signature(ElasticModel.elastic_create).parameters)

    assert documented_params == actual_params


    