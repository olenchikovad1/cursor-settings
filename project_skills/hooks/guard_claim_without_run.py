"""«Готово» не произносится без прогона.

# skill: quality-verification
# events: postToolUse, afterAgentResponse, stop

Правило говорит: не заявлять, что работает, исправлено или готово, без
команды, которая это доказывает. Уверенное рассуждение выглядит ровно так же,
как факт, и человек узнаёт разницу, когда открывает приложение сам.

Как это устроено в Cursor. Транскрипта, который удобно разбирать, нет, зато
у каждого события есть `generation_id` — он меняется с каждым сообщением
человека, то есть совпадает с «этим ходом». Поэтому:

- после команды оболочки хук отмечает ход, если команда похожа на прогон
  (`proof_commands` в `rails.json`);
- после ответа агента запоминает его текст;
- на `stop` сверяет: было ли в ходе заявление о готовности и был ли прогон.

Нарушение возвращается сообщением, которое Cursor подаёт следующим ходом
(`followup_message`). Это напоминание, а не остановка: бывает работа, где
прогонять нечего, и тогда достаточно сказать, что проверка была ручной.
`loop_limit: 1` в `hooks.json` не даёт ему повториться по кругу.

Главное требование — **редкость**. Заявление распознаётся узко: только
утверждение о законченности, без «осталось», «будет» и пересказа чужих слов.
"""

import re

import _rails_common as g

DEFAULT_PROOF = [
    "pytest", "unittest", "npm test", "npm run test", "yarn test", "pnpm test",
    "go test", "cargo test", "make test", "make check", "tox", "dotnet test",
    "vitest", "jest", "playwright", "ruff", "mypy", "eslint", "tsc",
]

CLAIM = re.compile(
    r"(?:^|[.!?»)\]\n]\s*|\A)\s*"
    r"(готово\b"
    r"|всё\s+работает\b|все\s+работает\b"
    r"|теперь\s+работает\b"
    r"|(?:баг|ошибка|проблема)\s+(?:починен|починена|исправлен|исправлена)\b"
    r"|исправила?\s*[—-]|починила?\s*[—-]"
    r"|проверка\s+проходит\b"
    r")",
    re.IGNORECASE)

NOT_A_CLAIM = re.compile(
    r"(осталось|будет\s+готово|почти|пока\s+не|ещё\s+не|еще\s+не"
    r"|ты\s+пис|вы\s+писа|по\s+твоим\s+словам|как\s+ты\s+сказал"
    r"|проверил[аи]?\s+вручную|проверк[аи]\s+был[аи]\s+ручн"
    r"|вручную\s+в\s+интерфейсе|автопрогона\s+здесь\s+не)",
    re.IGNORECASE)


def _turn(request: dict, state: dict) -> dict:
    turns = state.setdefault("claim_turns", {})
    key = str(request.get("generation_id") or "current")
    if key not in turns:
        # Держим только последние ходы: старые ничего не доказывают про этот.
        for old in list(turns)[:-4]:
            turns.pop(old, None)
        turns[key] = {"proof": False, "text": ""}
    return turns[key]


def after_shell(request: dict, state: dict) -> dict | None:
    command = g.shell_command(request).lower()
    if not command:
        return None
    proof = g.setting(request, "proof_commands", DEFAULT_PROOF)
    if any(marker.lower() in command for marker in proof):
        _turn(request, state)["proof"] = True
    return None


def after_response(request: dict, state: dict) -> dict | None:
    text = request.get("text") or ""
    if text:
        turn = _turn(request, state)
        turn["text"] = (turn["text"] + "\n" + text)[-20000:]
    return None


def on_stop(request: dict, state: dict) -> dict | None:
    if request.get("status") not in (None, "completed"):
        return None
    if int(request.get("loop_count") or 0) > 0:
        return None
    turn = _turn(request, state)
    text = turn.get("text", "")
    if not text or not CLAIM.search(text) or NOT_A_CLAIM.search(text) or turn.get("proof"):
        return None
    return {"followup_message": (
        "quality-verification: в этом ходе заявлена готовность, но не было ни одного "
        "прогона. Назови команду, которая это доказывает, и прогони её. Если прогонять "
        "нечего — документ, план, конфигурация, — скажи, что проверка была ручной или "
        "что автопрогона здесь не бывает.")}


HANDLES = {
    "postToolUse": after_shell,
    "afterAgentResponse": after_response,
    "stop": on_stop,
}
