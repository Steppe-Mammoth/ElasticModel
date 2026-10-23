"""Генератор docs/demo/comparison.gif для README: pydantic.BaseModel проти ElasticModel.

Кадри малює Pillow, а всі виводи (ValidationError, 'abc', NotLoadedFieldError) беруться з реального
прогону коду на справжніх pydantic та elasticmodel. Якщо поведінка розійдеться зі сценарієм
(`Step.expect`), скрипт упаде, а не намалює те, чого бібліотека не робить.

Запуск з кореня репозиторію:
    PYTHONPATH=. uv run --no-project --with pillow --with pydantic python docs/demo/make_comparison_gif.py

Екран: ліворуч постійно лежать моделі й дані, праворуч іде флоу, який з ними працює.
Заголовок флоу не зникає, поки триває його сцена.
"""

import json
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ValidationError

from elasticmodel import ElasticModel

OUTPUT = Path(__file__).with_name("comparison.gif")
FONT_DIR = Path("/usr/share/fonts/TTF")

# --- Дані демо -------------------------------------------------------------------------------------

# Запис-проєкція: поля `created.by` немає.
DOC = {"email": "a@b.com", "age": 30, "created": {"at": "2025-08-15"}}
# Той самий запис, але `age` прийшов текстом, хоча в моделі це int.
BAD_DOC = {**DOC, "age": "abc"}


def make_user_model(base: type) -> type:
    """Будує однакову пару моделей на переданій базі (BaseModel чи ElasticModel).

    Той самий код показується в лівій панелі кадрів, тому класи там мають ті самі поля.
    Метод `datetime_from_at` (як у README) показує різницю між вкладеним класом і сирим dict.
    """

    class Created(base):
        at: str
        by: str

        def datetime_from_at(self) -> datetime:
            return datetime.strptime(self.at, "%Y-%m-%d")

    class User(base):
        email: str
        age: int
        created: Created

    return User


# --- Виконання кроків сценарію ---------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    kind: str  # "ok" або "error"
    text: str  # що надрукував би REPL; порожньо для присвоєння


def run_code(code: str, namespace: dict) -> Outcome:
    """Виконує рядок коду в просторі імен і повертає те, що показав би REPL."""
    try:
        compiled = compile(code, "<demo>", "eval")
    except SyntaxError:
        compiled = compile(code, "<demo>", "exec")  # присвоєння: eval поверне None

    try:
        value = eval(compiled, namespace)
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        message = f"{where}: {first['msg']}" if where else first["msg"]
        return Outcome("error", f"ValidationError: {message}")
    except Exception as error:
        return Outcome("error", f"{type(error).__name__}: {error}")
    return Outcome("ok", "" if value is None else repr(value))


@dataclass(frozen=True)
class Step:
    code: str
    expect: str = "ok"  # "ok" або "error"; звіряється з реальним прогоном
    color: tuple | None = None  # колір виводу; за замовчуванням червоний для помилки, звичайний для успіху
    comment: str = ""
    comment_color: tuple | None = None
    circle: tuple | None = None  # колір обведення виводу; обводиться після короткої паузи


# --- Кольори, шрифти, геометрія --------------------------------------------------------------------

BG = (13, 17, 23)
PANEL = (22, 27, 34)
BORDER = (48, 54, 61)
CAPTION_BG = (30, 36, 44)
FG = (201, 209, 217)
DIM = (125, 133, 144)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
YELLOW = (210, 153, 34)

FONT = ImageFont.truetype(str(FONT_DIR / "Hack-Regular.ttf"), 15)
FONT_BOLD = ImageFont.truetype(str(FONT_DIR / "Hack-Bold.ttf"), 15)
FONT_TITLE = ImageFont.truetype(str(FONT_DIR / "Hack-Bold.ttf"), 21)
CHAR_W = FONT.getlength("M")

W = 1000
PAD, GAP = 24, 16
PANEL_TOP, PANEL_BOTTOM = 24, 540
CAPTION_TOP = PANEL_BOTTOM + 16
LEFT_W = 372
RIGHT_X = PAD + LEFT_W + GAP
RIGHT_W = W - PAD - RIGHT_X
HEAD_H = 64
LINE_H = 22
CAPTION_ROWS = 4  # скільки рядків підписів вміщається в стек одного флоу
CAPTION_H = CAPTION_ROWS * LINE_H + 32
H = CAPTION_TOP + CAPTION_H + 24
LEFT_COLS = int((LEFT_W - 28) / CHAR_W)
RIGHT_COLS = int((RIGHT_W - 28) / CHAR_W)
CAPTION_COLS = int((W - 2 * PAD - 28) / CHAR_W)

# Швидкість анімації
CHARS_PER_FRAME = 4
TYPE_MS = 35
PAUSE_CODE_MS = 350
PAUSE_OUT_MS = 1100
PAUSE_CIRCLE_MS = 600
PAUSE_SCENE_MS = 3200  # порожній флоу з новим заголовком: межа між сценами


# --- Модель екрана ---------------------------------------------------------------------------------


@dataclass
class Part:
    """Шматок рядка одного кольору; `circle` — колір обведення навколо нього."""

    text: str
    color: tuple
    circle: tuple | None = None


@dataclass
class Line:
    parts: list[Part]


def line(text: str, color: tuple = FG) -> Line:
    return Line([Part(text, color)])


@dataclass
class Caption:
    text: str
    color: tuple
    icon: str = ""  # "cry"


@dataclass
class Screen:
    data: list[Line] = field(default_factory=list)  # ліва панель, лишається між сценами
    flow: list[Line] = field(default_factory=list)  # права панель, очищується на початку сцени
    title: str = ""
    title_color: tuple = FG
    marks: tuple = ()  # "check" | "cross" | "warn"
    captions: list[Caption] = field(default_factory=list)  # стек підписів поточного флоу


# --- Малювання -------------------------------------------------------------------------------------


def draw_mark(draw: ImageDraw.ImageDraw, kind: str, x: int, y: int) -> None:
    """Малює значок 28x28 векторно, щоб не залежати від емодзі-шрифтів."""
    if kind == "check":
        draw.line([(x + 3, y + 14), (x + 11, y + 22), (x + 25, y + 4)], fill=GREEN, width=5, joint="curve")
    elif kind == "cross":
        draw.line([(x + 4, y + 4), (x + 24, y + 24)], fill=RED, width=5)
        draw.line([(x + 24, y + 4), (x + 4, y + 24)], fill=RED, width=5)
    elif kind == "warn":
        draw.polygon([(x + 14, y + 1), (x + 28, y + 26), (x, y + 26)], fill=YELLOW)
        draw.text((x + 10, y + 8), "!", font=FONT_BOLD, fill=BG)


def draw_cry(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    """Плачучий смайлик 24x24: жовте коло, очі, сумний рот, дві сльози."""
    draw.ellipse((x, y, x + 24, y + 24), fill=YELLOW)
    draw.ellipse((x + 6, y + 7, x + 9, y + 10), fill=BG)
    draw.ellipse((x + 15, y + 7, x + 18, y + 10), fill=BG)
    draw.arc((x + 6, y + 14, x + 18, y + 23), start=200, end=340, fill=BG, width=2)
    draw.ellipse((x + 4, y + 11, x + 8, y + 19), fill=(88, 166, 255))
    draw.ellipse((x + 16, y + 11, x + 20, y + 19), fill=(88, 166, 255))


def draw_lines(draw: ImageDraw.ImageDraw, lines: list[Line], x: int, y: int, cols: int) -> None:
    """Малює рядки; одно-частинні переносить по ширині, багаточастинні не переносить.

    Обведення одно-частинного рядка охоплює весь його перенесений блок.
    """
    for item in lines:
        if len(item.parts) == 1:
            part = item.parts[0]
            rows = textwrap.wrap(
                part.text, cols, subsequent_indent="    ", drop_whitespace=False
            ) or [""]
            top = y
            for row in rows:
                draw.text((x, y), row, font=FONT, fill=part.color)
                y += LINE_H
            if part.circle:
                widest = max(FONT.getlength(row) for row in rows)
                draw.rounded_rectangle(
                    (x - 6, top - 3, x + widest + 6, y - 3), radius=10, outline=part.circle, width=3
                )
            continue

        cursor = x
        for part in item.parts:
            width = FONT.getlength(part.text)
            draw.text((cursor, y), part.text, font=FONT, fill=part.color)
            if part.circle:
                draw.rounded_rectangle(
                    (cursor - 6, y - 3, cursor + width + 6, y + LINE_H - 3),
                    radius=10, outline=part.circle, width=3,
                )
            cursor += width
        y += LINE_H


def render(screen: Screen) -> Image.Image:
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)

    # Дві панелі: моделі й дані ліворуч, флоу праворуч
    draw.rounded_rectangle((PAD, PANEL_TOP, PAD + LEFT_W, PANEL_BOTTOM), 8, fill=PANEL, outline=BORDER)
    draw.rounded_rectangle((RIGHT_X, PANEL_TOP, RIGHT_X + RIGHT_W, PANEL_BOTTOM), 8, fill=PANEL, outline=BORDER)
    draw.text((PAD + 14, PANEL_TOP + 16), "MODELS + DATA  ·  projection record", font=FONT, fill=DIM)
    draw.line((PAD + 8, PANEL_TOP + 42, PAD + LEFT_W - 8, PANEL_TOP + 42), fill=BORDER)
    draw.line((RIGHT_X + 8, PANEL_TOP + HEAD_H - 6, RIGHT_X + RIGHT_W - 8, PANEL_TOP + HEAD_H - 6), fill=BORDER)

    # Заголовок флоу: значки + назва, видимі всю сцену
    cursor = RIGHT_X + 16
    for mark in screen.marks:
        draw_mark(draw, mark, cursor, PANEL_TOP + 14)
        cursor += 36
    draw.text((cursor + 2, PANEL_TOP + 15), screen.title, font=FONT_TITLE, fill=screen.title_color)

    draw_lines(draw, screen.data, PAD + 14, PANEL_TOP + 54, LEFT_COLS)
    draw_lines(draw, screen.flow, RIGHT_X + 14, PANEL_TOP + HEAD_H + 6, RIGHT_COLS)

    # Підписи знизу на всю ширину: стек, найновіший угорі. Номер відповідає порядку появи (1 — найстаріший)
    if screen.captions:
        draw.rounded_rectangle((PAD, CAPTION_TOP, W - PAD, H - 24), 8, fill=CAPTION_BG, outline=BORDER)
        y = CAPTION_TOP + 16
        used_rows = 0
        for number, caption in reversed(list(enumerate(screen.captions, start=1))):
            prefix = f"{number}. "
            rows = textwrap.wrap(
                caption.text, CAPTION_COLS, initial_indent=prefix, subsequent_indent=" " * len(prefix)
            )
            used_rows += len(rows)
            if used_rows > CAPTION_ROWS:
                raise SystemExit(f"Підписи флоу не вміщаються в {CAPTION_ROWS} рядки: збільште CAPTION_ROWS")
            for row in rows:
                draw.text((PAD + 14, y), row, font=FONT, fill=caption.color)
                y += LINE_H
            if caption.icon == "cry":
                draw_cry(draw, int(PAD + 14 + len(rows[-1]) * CHAR_W + 14), y - LINE_H - 2)
    return image


# --- Аніматор --------------------------------------------------------------------------------------


class Animator:
    """Збирає кадри: кожна зміна екрана — це `snap` із тривалістю показу."""

    def __init__(self) -> None:
        self.screen = Screen()
        self.frames: list[tuple[Image.Image, int]] = []

    def snap(self, ms: int) -> None:
        self.frames.append((render(self.screen), ms))

    def type_line(self, lines: list[Line], text: str) -> None:
        """Додає рядок і «друкує» його по кілька символів за кадр."""
        item = line("")
        lines.append(item)
        for end in range(CHARS_PER_FRAME, len(text) + CHARS_PER_FRAME, CHARS_PER_FRAME):
            item.parts[0].text = text[:end]
            self.snap(TYPE_MS)

    def run_step(self, namespace: dict, step: Step) -> Part | None:
        """Друкує код кроку, виконує його по-справжньому і показує вивід. Повертає частину з виводом."""
        outcome = run_code(step.code, namespace)
        if outcome.kind != step.expect:
            raise SystemExit(
                f"Сценарій розійшовся з реальністю: {step.code!r} дав {outcome.kind} "
                f"({outcome.text!r}), а очікувалось {step.expect}"
            )

        self.type_line(self.screen.flow, ">>> " + step.code)
        self.snap(PAUSE_CODE_MS)

        color = step.color or (RED if outcome.kind == "error" else FG)
        comment_color = step.comment_color or color
        shown = None
        if outcome.text:
            shown = Part(outcome.text, color)
            parts = [shown]
            if step.comment:
                parts.append(Part("   " + step.comment, comment_color))
            self.screen.flow.append(Line(parts))
        elif step.comment:
            self.screen.flow.append(line(step.comment, comment_color))
        self.snap(PAUSE_OUT_MS)

        if shown and step.circle:
            shown.circle = step.circle
            self.snap(PAUSE_CIRCLE_MS)
        return shown

    def new_scene(self, title: str, title_color: tuple, marks: tuple) -> None:
        """Починає сцену: очищує флоу й підпис, ставить заголовок, який тримається всю сцену.

        Короткий показ порожнього флоу з новим заголовком відділяє сцени одну від одної.
        """
        self.screen.flow = []
        self.screen.captions = []
        self.screen.title, self.screen.title_color, self.screen.marks = title, title_color, marks
        self.snap(PAUSE_SCENE_MS)

    def say(self, text: str, color: tuple = FG, icon: str = "", ms: int = 2600) -> None:
        """Додає підпис у стек поточного флоу: попередні лишаються, стек очищується в `new_scene`.

        Номер підпису — його порядок появи в флоу; на екрані найновіший стоїть угорі.
        """
        self.screen.captions.append(Caption(text, color, icon))
        self.snap(ms)


# --- Сценарій --------------------------------------------------------------------------------------


def build_scenes() -> list[tuple[Image.Image, int]]:
    pydantic_ns = {"User": make_user_model(BaseModel), "doc": DOC, "bad_doc": BAD_DOC}
    elastic_ns = {"User": make_user_model(ElasticModel), "doc": DOC, "bad_doc": BAD_DOC}
    anim = Animator()
    screen = anim.screen

    # Сцена 1: моделі та запис-проєкція, у якому явно позначено відсутнє поле.
    anim.snap(800)
    created_base, user_base = Part("BaseModel", DIM), Part("BaseModel", DIM)
    declared_int, declared_created = Part("int", DIM), Part("Created", DIM)
    missing_field = Part('# "by": ... is not loaded', YELLOW)
    method_def = Part("def datetime_from_at(self): ...", DIM)  # тіло скорочено, щоб рядок влазив у панель
    screen.data += [
        Line([Part("class Created(", DIM), created_base, Part("):", DIM)]),
        line("    at: str", DIM),
        line("    by: str", DIM),
        Line([Part("    ", DIM), method_def]),
        line(""),
        Line([Part("class User(", DIM), user_base, Part("):", DIM)]),
        line("    email: str", DIM),
        Line([Part("    age: ", DIM), declared_int]),
        Line([Part("    created: ", DIM), declared_created]),
        line(""),
        line("doc = {"),
    ]
    anim.snap(900)
    for key, value in DOC.items():
        if isinstance(value, dict):
            # Вкладений запис розгорнуто, щоб відсутнє поле стояло прямо в ньому
            screen.data.append(line(f"  {json.dumps(key)}: {{"))
            for inner_key, inner_value in value.items():
                screen.data.append(line(f"    {json.dumps(inner_key)}: {json.dumps(inner_value)},"))
            screen.data.append(Line([Part("    ", YELLOW), missing_field]))
            screen.data.append(line("  },"))
        else:
            screen.data.append(line(f"  {json.dumps(key)}: {json.dumps(value)},"))
        anim.snap(350)
    screen.data.append(line("}"))
    anim.say(
        "We have a record with missing fields. That's fine: we know it and don't need them. "
        "It's a projection, it saves memory.",
        ms=5500,
    )

    # Сцена 2: pydantic.BaseModel не приймає неповні дані.
    anim.new_scene("pydantic.BaseModel", RED, ("cross",))
    anim.run_step(pydantic_ns, Step("User.model_validate(doc)", expect="error"))
    anim.say("Oops, we can't build it: full data or nothing", RED, icon="cry", ms=4500)

    # Сцена 3: model_construct, два прогони (валідні й невалідні дані).
    anim.new_scene("pydantic.BaseModel.model_construct", YELLOW, ("warn", "cross"))
    anim.say("OK, let's try model_construct: it lets us create the object, but validates nothing", YELLOW, ms=3500)
    anim.run_step(pydantic_ns, Step("user = User.model_construct(**doc)"))
    anim.run_step(pydantic_ns, Step("user.age"))

    # model_construct не будує вкладені моделі: created лишається dict, тож методу Created немає.
    # Жовтим позначаємо розбіжність типів (вивід + задекларований тип у моделі)
    # і наслідок: помилку виклику методу та сам метод у моделі.
    anim.run_step(
        pydantic_ns,
        Step("type(user.created).__name__", circle=YELLOW, comment="# declared Created, got dict", comment_color=YELLOW),
    )
    declared_created.circle = YELLOW
    anim.snap(PAUSE_CIRCLE_MS)
    anim.run_step(pydantic_ns, Step("user.created.datetime_from_at()", expect="error", circle=YELLOW))
    method_def.circle = YELLOW
    anim.say("Oops, nested models are not built: created is a plain dict, so its methods are gone", RED, ms=5500)
    method_def.circle = None  # жовте обведення Created лишається до кінця флоу для наочності

    bad_age = Part(json.dumps(BAD_DOC["age"]), FG)
    screen.data += [
        line(""),
        Line([Part('bad_doc = {**doc, "age": ', FG), bad_age, Part("}", FG)]),
    ]
    anim.say("Same record, but now age arrives as text", FG, ms=3000)
    screen.flow.append(line(""))  # візуально відокремлює прогін з bad_doc від прогону з doc
    anim.run_step(pydantic_ns, Step("bad_user = User.model_construct(**bad_doc)"))
    anim.run_step(
        pydantic_ns,
        Step("bad_user.age", circle=RED, comment="# declared int, got str", comment_color=RED),
    )
    declared_int.circle = RED  # обводимо лише задекларований тип; значення 'abc' уже обведене у виводі
    # Фінальна затримка флоу 2 довша на 3 с: це пауза між флоу 2 і 3, повний стек підписів і обведення на екрані
    anim.say("Oops, an invalid type slipped through: age is declared int, but silently holds 'abc'", RED, ms=8500)

    # Сцена 4: ElasticModel, ті самі два прогони, але завантажені дані валідуються.
    # Ліву панель оновлюємо до new_scene: кадр-межа вже показує нову сцену без залишків флоу 2
    # (моделі тепер успадковують ElasticModel, червоного обведення int більше немає).
    declared_int.circle = declared_created.circle = None
    created_base.text = user_base.text = "ElasticModel"
    anim.new_scene("elasticmodel.ElasticModel", GREEN, ("check",))
    anim.run_step(elastic_ns, Step("user = User.elastic_create(doc)", comment="# partial data accepted", comment_color=GREEN))
    anim.run_step(elastic_ns, Step("user.age"))
    # Той самий тип-запит, що й у model_construct, але тут вкладена модель справді побудована
    anim.run_step(elastic_ns, Step("type(user.created).__name__", color=GREEN, circle=GREEN))
    anim.run_step(elastic_ns, Step("user.created.datetime_from_at()", color=GREEN))
    anim.say("Nested models are built, so their methods work on the loaded data", GREEN, ms=3500)
    # Звернення до незавантаженого поля: обводимо і вивід, і саме поле в даних зліва
    anim.run_step(elastic_ns, Step("user.created.by", expect="error", color=YELLOW, circle=YELLOW))
    missing_field.circle = YELLOW
    anim.say("Reading a field that was not loaded raises an error instead of returning a made-up value", YELLOW, ms=5000)
    screen.flow.append(line(""))
    anim.run_step(elastic_ns, Step("bad_user = User.elastic_create(bad_doc)", expect="error", color=GREEN, circle=GREEN))
    # Обводимо те значення в bad_doc, яке ElasticModel упіймав, і тип, якому воно не відповідає
    bad_age.circle = declared_int.circle = GREEN
    anim.say("The loaded data is validated: the bad type is caught", GREEN, ms=3500)
    anim.say("Create an object from limited data, and still validate what is loaded", GREEN, ms=12000)

    return anim.frames


def save_gif(frames: list[tuple[Image.Image, int]]) -> None:
    """Зберігає GIF зі спільною палітрою: різниця між кадрами лишається малою, файл — легким."""
    step = max(1, len(frames) // 14)
    sample = Image.new("RGB", (W, H * len(frames[::step])))
    for index, (image, _) in enumerate(frames[::step]):
        sample.paste(image, (0, index * H))
    palette = sample.quantize(colors=48, method=Image.Quantize.MEDIANCUT)

    images = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image, _ in frames]
    durations = [ms for _, ms in frames]
    images[0].save(
        OUTPUT, save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True
    )


def main() -> None:
    frames = build_scenes()
    save_gif(frames)
    size_kb = OUTPUT.stat().st_size / 1024
    seconds = sum(ms for _, ms in frames) / 1000
    print(f"{OUTPUT}: {len(frames)} кадрів, {seconds:.1f} с, {size_kb:.0f} КБ")


if __name__ == "__main__":
    main()
