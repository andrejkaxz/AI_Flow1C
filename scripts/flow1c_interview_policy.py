"""Deterministic contracts for interview drafts; no workbook or project I/O."""
from __future__ import annotations

from typing import Any

COLUMNS = {
    "l1_code": "Код уровня 1", "l1_name": "Процесс 1 уровня",
    "l2_code": "Код уровня 2", "l2_name": "Процесс 2 уровня",
    "l3_code": "Код уровня 3", "l3_name": "Процесс 3 уровня",
    "question": "Что уточняем на интервью",
    "owner": "Владелец процесса / правила",
    "interviewer": "Исполнитель / участник интервью",
    "answer": "Ответ / решение", "requirement": "Требование / критерий приёмки",
    "question_id": "ID вопроса", "decision": "Нормализованное решение",
    "source": "Источник / исходный номер вопроса",
}
REQUIRED = tuple(list(COLUMNS)[:7])
PATCHABLE = {"owner", "interviewer", "answer", "decision", "requirement", "source"}
MAX_ROWS = 10000


class InterviewError(ValueError):
    def __init__(self, code: str, message: str, next_action: str = "Исправьте указанные поля и повторите действие; исходная книга сохранена.") -> None:
        super().__init__(message)
        self.code = code
        self.next_action = next_action

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "component": "interview-register", "message": str(self),
                "recoverable": True, "next_action": self.next_action}


def text_value(value: Any) -> str:
    if not isinstance(value, str):
        raise InterviewError("INTERVIEW_INVALID_INPUT", "Значения ячеек должны быть строками; коды храните текстом.")
    if len(value) > 32767 or any(ord(ch) < 32 and ch not in "\n\r\t" for ch in value):
        raise InterviewError("INTERVIEW_INVALID_INPUT", "Текст превышает предел Excel или содержит управляющие символы.")
    return value


def validate_rows(rows: Any, *, require_ids: bool = True) -> list[dict[str, str]]:
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise InterviewError("INTERVIEW_INVALID_INPUT", f"rows должен быть списком не более {MAX_ROWS} строк.")
    result = []
    ids: set[str] = set()
    hierarchy: dict[tuple[int, str], tuple[str, ...]] = {}
    questions: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, 2):
        if not isinstance(row, dict) or set(row) - set(COLUMNS):
            raise InterviewError("INTERVIEW_INVALID_INPUT", f"Строка {index}: неизвестные поля.")
        clean = {key: text_value(value) for key, value in row.items()}
        missing = [key for key in REQUIRED if not clean.get(key, "").strip()]
        if require_ids and not clean.get("question_id", "").strip():
            missing.append("question_id")
        if missing:
            raise InterviewError("INTERVIEW_HIERARCHY_INVALID", f"Строка {index}: заполните {', '.join(missing)}.")
        question_id = clean.get("question_id", "")
        if question_id and question_id in ids:
            raise InterviewError("INTERVIEW_DUPLICATE_ID", f"Повтор ID вопроса: {question_id}.")
        if question_id:
            ids.add(question_id)
        for level in (1, 2, 3):
            key = (level, clean[f"l{level}_code"])
            identity = tuple(clean[field] for previous in range(1, level) for field in (f"l{previous}_code", f"l{previous}_name")) + (clean[f"l{level}_name"],)
            if key in hierarchy and hierarchy[key] != identity:
                raise InterviewError("INTERVIEW_HIERARCHY_CONFLICT", f"Код {key[1]} имеет разные названия или родителей.")
            hierarchy[key] = identity
        question_key = (clean["l3_code"], " ".join(clean["question"].casefold().split()))
        if question_key in questions:
            raise InterviewError("INTERVIEW_DUPLICATE_QUESTION", f"Повтор вопроса для процесса {clean['l3_code']}.")
        questions.add(question_key)
        result.append(clean)
    return result


def apply_changes(existing: list[dict[str, str]], additions: Any, patches: Any) -> list[dict[str, str]]:
    """Only explicitly supplied answer fields change; process/question identity is fixed."""
    result = [dict(row) for row in existing]
    by_id = {row["question_id"]: row for row in result}
    if not isinstance(patches, list) or len(patches) > MAX_ROWS:
        raise InterviewError("INTERVIEW_INVALID_INPUT", "patches должен быть ограниченным списком.")
    patched: set[str] = set()
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) != {"question_id", "fields"}:
            raise InterviewError("INTERVIEW_INVALID_INPUT", "Правка требует question_id и fields.")
        question_id = text_value(patch["question_id"])
        fields = patch["fields"]
        if question_id not in by_id or question_id in patched:
            raise InterviewError("INTERVIEW_UNKNOWN_QUESTION", f"Неизвестный или повторно исправляемый вопрос: {question_id}.")
        if not isinstance(fields, dict) or not fields or set(fields) - PATCHABLE:
            raise InterviewError("INTERVIEW_UNSAFE_PATCH", "Правка не может менять коды, названия и исходный вопрос; создайте таблицу соответствия отдельно.")
        by_id[question_id].update({key: text_value(value) for key, value in fields.items()})
        patched.add(question_id)
    added = validate_rows(additions)
    for item in [*added, *(by_id[question_id] for question_id in patched)]:
        if item.get("requirement", "").strip() and not item.get("answer", "").strip():
            raise InterviewError("INTERVIEW_REQUIREMENT_UNSOURCED", "Новое или изменённое требование в основной строке требует сохранённого ответа.")
    result.extend(added)
    return validate_rows(result)


def validate_requirements(requirements: Any, rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if not isinstance(requirements, list) or len(requirements) > MAX_ROWS:
        raise InterviewError("INTERVIEW_INVALID_INPUT", "requirements должен быть ограниченным списком.")
    allowed = {"draft_id", "question_id", "text", "acceptance_criterion", "owner", "priority", "implementation", "evidence", "status"}
    by_id = {row["question_id"]: row for row in rows}
    result = []
    ids: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, dict) or set(requirement) - allowed:
            raise InterviewError("INTERVIEW_INVALID_INPUT", "Неизвестные поля требования.")
        clean = {key: text_value(value) for key, value in requirement.items()}
        if any(not clean.get(key, "").strip() for key in ("draft_id", "question_id", "text", "acceptance_criterion")):
            raise InterviewError("INTERVIEW_REQUIREMENT_INVALID", "Требуются draft_id, question_id, text и acceptance_criterion.")
        row = by_id.get(clean["question_id"])
        if not row or not row.get("answer", "").strip():
            raise InterviewError("INTERVIEW_REQUIREMENT_UNSOURCED", "Требование должно ссылаться на вопрос с сохранённым ответом; гипотезы помещайте в открытые вопросы.")
        if clean["draft_id"] in ids:
            raise InterviewError("INTERVIEW_DUPLICATE_ID", "Повтор draft_id требования.")
        ids.add(clean["draft_id"])
        clean.setdefault("status", "Требует согласования")
        if clean.get("implementation") == "Типовой механизм подтверждён" and not clean.get("evidence", "").strip():
            raise InterviewError("INTERVIEW_EVIDENCE_REQUIRED", "Подтверждённый типовой механизм требует основания проверки продукта и релиза.")
        if clean["status"] not in {"Черновик", "Требует согласования"}:
            raise InterviewError("INTERVIEW_APPROVAL_FORBIDDEN", "Реестр интервью не утверждает требования и не меняет формальный статус.")
        result.append({**clean, "l3_code": row["l3_code"], "answer": row["answer"]})
    return result
