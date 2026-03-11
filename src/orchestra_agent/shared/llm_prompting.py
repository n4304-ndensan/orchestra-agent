from __future__ import annotations

import json
from typing import Any, Literal, cast

type LlmLanguage = Literal["en", "ja", "zh", "es", "vi"]
type PromptKind = Literal["planner", "proposal", "executor"]

_LANGUAGE_NOTES: dict[LlmLanguage, str] = {
    "en": (
        "Use English for natural-language reasoning, summaries, and free-text fields. "
        "Keep JSON keys, tool names, and protocol field names exactly as specified."
    ),
    "ja": (
        "自然言語の説明・要約・自由記述は日本語で行ってください。"
        "JSON のキー名、tool 名、protocol の field 名は指定どおり変更しないでください。"
    ),
    "zh": (
        "自然语言说明、摘要和自由文本请使用中文。"
        "JSON 键名、工具名和协议字段名必须严格保持原样。"
    ),
    "es": (
        "Usa espanol para el razonamiento en lenguaje natural, los resumenes y los textos libres. "
        "No cambies las claves JSON, los nombres de herramientas ni los campos del protocolo."
    ),
    "vi": (
        "Hay dung tieng Viet cho phan giai thich, tom tat va cac truong van ban tu do. "
        "Khong duoc thay doi khoa JSON, ten cong cu hoac ten truong cua giao thuc."
    ),
}

_MEMORY_NOTES_TRUE: dict[LlmLanguage, str] = {
    "en": (
        "The runtime assumes this model remembers prior turns in the active conversation. "
        "Do not ask to resend unchanged context unless it is truly missing."
    ),
    "ja": (
        "この runtime は、現在の会話で model が前の turn を記憶している前提です。"
        "不足していない限り、変わっていない context の再送を要求しないでください。"
    ),
    "zh": "当前 runtime 假设模型会记住本会话中的前文。除非上下文确实缺失，否则不要要求重复发送未变化的信息。",
    "es": (
        "Este runtime asume que el modelo recuerda los turnos previos de la conversacion activa. "
        "No pidas reenviar contexto sin cambios salvo que realmente falte."
    ),
    "vi": (
        "Runtime nay gia dinh model nho duoc cac luot truoc trong cung mot hoi thoai. "
        "Khong yeu cau gui lai context khong thay doi tru khi no thuc su bi thieu."
    ),
}

_MEMORY_NOTES_FALSE: dict[LlmLanguage, str] = {
    "en": (
        "The runtime assumes this model does not remember prior turns unless they are included in "
        "the current request. Rely only on the messages you receive in this request."
    ),
    "ja": (
        "この runtime は、現在の request に含まれていない前の turn を model が記憶していない前提です。"
        "この request に含まれる message のみを根拠にしてください。"
    ),
    "zh": "当前 runtime 假设模型不会记住未包含在本次请求中的前文。请只依据当前请求中的消息作答。",
    "es": (
        "Este runtime asume que el modelo no recuerda los turnos previos si no vienen incluidos "
        "en la solicitud actual. Basate solo en los mensajes de esta solicitud."
    ),
    "vi": (
        "Runtime nay gia dinh model khong nho duoc cac luot truoc neu chung khong nam trong "
        "request hien tai. Chi duoc dua vao cac message co trong request nay."
    ),
}

_EXECUTOR_RECOVERY_NOTES: dict[LlmLanguage, str] = {
    "en": (
        "If you receive runtime_error, correct only the last runtime action. "
        "Do not return a workflow step plan, do not return top-level steps, and do not restart the whole workflow."
    ),
    "ja": (
        "runtime_error を受け取った場合は、直前の runtime action だけを修正してください。"
        "workflow の step plan や top-level の steps を返してはいけません。workflow 全体をやり直さないでください。"
    ),
    "zh": "如果收到 runtime_error，只修正上一条 runtime action。不要返回 workflow step plan、不要返回顶层 steps，也不要重启整个 workflow。",
    "es": (
        "Si recibes runtime_error, corrige solo la ultima runtime action. "
        "No devuelvas un step plan del workflow, no devuelvas steps de nivel superior y no reinicies todo el workflow."
    ),
    "vi": (
        "Neu nhan runtime_error, chi sua runtime action gan nhat. "
        "Khong duoc tra ve step plan cua workflow, khong tra ve top-level steps va khong khoi dong lai toan bo workflow."
    ),
}

_RUNTIME_ERROR_INSTRUCTIONS: dict[LlmLanguage, str] = {
    "en": (
        "Return one corrected runtime action JSON object for the current step only. "
        "Do not return workflow planning output."
    ),
    "ja": "現在の step に対する修正済み runtime action の JSON object を 1 つだけ返してください。workflow planning の出力は返さないでください。",
    "zh": "请只返回当前 step 的一个修正后 runtime action JSON object。不要返回 workflow planning 输出。",
    "es": "Devuelve solo un JSON object con la runtime action corregida para el step actual. No devuelvas salida de workflow planning.",
    "vi": "Chi tra ve mot JSON object chua runtime action da sua cho step hien tai. Khong duoc tra ve workflow planning output.",
}


def build_system_prompt(
    base_prompt: str,
    *,
    language: LlmLanguage,
    prompt_kind: PromptKind,
    remembers_context: bool | None = None,
) -> str:
    sections = [_LANGUAGE_NOTES[language]]
    if remembers_context is not None:
        sections.append(
            _MEMORY_NOTES_TRUE[language]
            if remembers_context
            else _MEMORY_NOTES_FALSE[language]
        )
    if prompt_kind == "executor":
        sections.append(_EXECUTOR_RECOVERY_NOTES[language])
    sections.append(base_prompt)
    return "\n\n".join(section for section in sections if section.strip())


def build_runtime_error_feedback(
    *,
    language: LlmLanguage,
    kind: str,
    error_message: str,
    model_output: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "runtime_error": {
            "kind": kind,
            "message": error_message,
            "instruction": _RUNTIME_ERROR_INSTRUCTIONS[language],
        }
    }
    if isinstance(model_output, str) and model_output.strip():
        payload["runtime_error"]["last_model_output"] = model_output
    return json.dumps(payload, ensure_ascii=False, indent=2)


def as_llm_language(value: Any, default: LlmLanguage = "en") -> LlmLanguage:
    if value is None:
        return default
    if value not in _LANGUAGE_NOTES:
        raise ValueError("llm.language must be one of: en, ja, zh, es, vi.")
    return cast(LlmLanguage, value)
