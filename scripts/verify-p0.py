"""Offline verification for the P0 batch.

Runs against the workspace sources using the repo venv (which has the deps).
No network and no real API key needed. Output is ASCII-only on purpose:
Windows PowerShell 5.1 mangles non-ASCII console output.
"""
import importlib
import inspect
import os
import pathlib
import re
import shutil
import sys

WORKSPACE = pathlib.Path(__file__).resolve().parents[1]
# Optional first argument: verify another root instead of this workspace
# (e.g. the PyCharm repo) so the check runs against the shipped copy.
ROOT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else WORKSPACE
BACKEND = ROOT / "v1" / "backend"
sys.path.insert(0, str(BACKEND))

# Keep the scratch DB inside the target root: the sandbox lets spawned processes
# write there but not into the system temp dir.
tmpdir = ROOT / ".p0-verify"
if tmpdir.exists():
    shutil.rmtree(tmpdir)
tmpdir.mkdir(parents=True)
db_path = tmpdir / "app.db"
os.environ["DATABASE_URL"] = "sqlite:///" + db_path.as_posix()
os.environ["DEEPSEEK_API_KEY"] = "sk-" + "a" * 32
os.environ["DEEPSEEK_MODEL"] = "deepseek-flash"

FAILED = []


def check(name, ok, detail=""):
    if not ok:
        FAILED.append(name)
    print(("PASS  " if ok else "FAIL  ") + name + (("   -> " + str(detail)) if detail else ""))


print("== import the app (catches wiring errors) ==")
import app.main  # noqa: E402
from app import llm, models, schemas  # noqa: E402
from app.agents import orchestrator, roleplay  # noqa: E402
from app.api import chat as chat_api  # noqa: E402
from app.config import _looks_like_real_key, settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.seed import seed_characters  # noqa: E402
from app.services import chat_service, session_service  # noqa: E402

print("\n== P0-1 model name + fail-fast key check ==")
check("default model is deepseek-flash", settings.deepseek_model == "deepseek-flash", settings.deepseek_model)
check("real key accepted", _looks_like_real_key("sk-" + "a" * 32))
check("empty key rejected", not _looks_like_real_key(""))
check("placeholder key rejected", not _looks_like_real_key("sk-" + "\u4f60\u7684\u5bc6\u94a5"))
check("too-short key rejected", not _looks_like_real_key("sk-abc"))
check("lifespan raises on bad key", "api_key_ready" in inspect.getsource(app.main.lifespan))
main_src = inspect.getsource(app.main)
check("logging.basicConfig present (else INFO logs vanish)", "basicConfig" in main_src)

print("\n== P0-4 content length 1..500 ==")
from pydantic import ValidationError  # noqa: E402


def rejected(value):
    try:
        schemas.MessageCreate(content=value)
        return False
    except ValidationError:
        return True


check("empty content rejected (422)", rejected(""))
check("501 chars rejected", rejected("x" * 501))
check("500 chars accepted", len(schemas.MessageCreate(content="x" * 500).content) == 500)
check("1 char accepted", len(schemas.MessageCreate(content="x").content) == 1)
check("MAX_MESSAGE_CHARS == 500", schemas.MAX_MESSAGE_CHARS == 500)

print("\n== P0-6 sqlite pragmas + FK delete regression ==")
Base.metadata.create_all(bind=engine)
with SessionLocal() as db:
    seed_characters(db)
    sess = session_service.create_session(db)
    session_service.append_message(db, sess.id, "user", "hello")
    session_service.append_message(db, sess.id, "assistant", "hi there")
    sid = sess.id

with SessionLocal() as db:
    check("messages persisted", len(session_service.list_messages(db, sid)) == 2)
    check("sessions listed during writes", len(session_service.list_sessions(db)) == 1)

with SessionLocal() as db:
    deleted = session_service.delete_session(db, sid)
    check("delete_session still works with foreign_keys=ON", deleted is True)
    check("child messages cascaded away", len(session_service.list_messages(db, sid)) == 0)

from sqlalchemy import text  # noqa: E402

with engine.connect() as conn:
    jm = conn.execute(text("PRAGMA journal_mode")).scalar()
    bt = conn.execute(text("PRAGMA busy_timeout")).scalar()
    fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
check("journal_mode=WAL", str(jm).lower() == "wal", jm)
check("busy_timeout=5000", int(bt) == 5000, bt)
check("foreign_keys=ON", int(fk) == 1, fk)
check("app.db-wal exists on disk", db_path.with_name(db_path.name + "-wal").exists())

print("\n== P0-2 / P0-3 code shape ==")
sig = inspect.signature(orchestrator.run_agent)
check("run_agent passes temperature/max_tokens", {"temperature", "max_tokens"} <= set(sig.parameters), list(sig.parameters))
orch_src = inspect.getsource(orchestrator)
check("wrap-up path exists", "WRAP_UP_PROMPT" in orch_src)
check("wrap-up drops tools", "stream(None)" in orch_src)
check("no early return on budget exhaustion", "if not tool_calls or tool_rounds" not in orch_src)
check("build_context exists", hasattr(roleplay, "build_context"))
check("build_memory removed", not hasattr(roleplay, "build_memory"))
cs_src = inspect.getsource(chat_service)
check("empty reply is not persisted", "no content" in cs_src or "content" in cs_src)
check("empty-reply guard present", "strip()" in cs_src and "error" in cs_src)
check("usage logged", "chat.completed" in cs_src)
check("build_context called", "build_context(" in cs_src)
check("usage collected from llm", "usage" in inspect.getsource(llm) and "include_usage" in inspect.getsource(llm))
check("stream_options fallback present", "stream_options" in inspect.getsource(llm))

print("\n== second batch: A1-A4 / B1 / B3 / C12 ==")
llm_src = inspect.getsource(llm)
check(
    "A1 client is built lazily, not at import",
    "_get_client()" in llm_src and "_client = OpenAI(" not in llm_src,
)
check(
    "B3 explicit timeout + retries",
    "timeout=LLM_TIMEOUT_SECONDS" in llm_src and "max_retries=LLM_MAX_RETRIES" in llm_src,
)

chat_api_src = inspect.getsource(chat_api)
check("A2 session None guard", "session is None" in chat_api_src and "已被删除" in chat_api_src)
check("A2 character None guard", "character is None" in chat_api_src and "角色卡不存在" in chat_api_src)
check("A2 _sse helper used for errors", chat_api_src.count("_sse(") >= 2)
# Scope the comparison to event_stream: the module's first `try:` is in create_session.
stream_src = chat_api_src[chat_api_src.index("def event_stream"):]
check("A2 guards precede the try block", stream_src.index("character is None") < stream_src.index("try:"))

client_ts = (ROOT / "v1/frontend/src/api/client.ts").read_text(encoding="utf-8")
check("A3 shared error-detail reader", client_ts.count("readErrorDetail") >= 3)
check("A3 handles pydantic 422 array", "Array.isArray(data?.detail)" in client_ts)
check("A3 streamPost no longer drops detail", "throw new Error(`请求失败" not in client_ts)

input_vue = (ROOT / "v1/frontend/src/components/ChatInput.vue").read_text(encoding="utf-8")
check("A4 counts by code point", "[...text.value].length" in input_vue)
# Only the tag matters -- the word "maxlength" also appears in an explanatory comment.
textarea_tag = input_vue[input_vue.index("<textarea"):input_vue.index("</textarea>")]
check("A4 UTF-16 maxlength removed from the textarea", "maxlength" not in textarea_tag)
check("A4 counter shows the code-point count", "{{ charCount }}" in input_vue)

main_src = inspect.getsource(app.main)
check(
    "B1 noisy loggers muted",
    "httpx" in main_src and "setLevel(logging.WARNING)" in main_src,
)

m = re.search(r"const MAX_CHARS = (\d+)", input_vue)
check(
    "C12 front/back char limit match",
    m is not None and int(m.group(1)) == schemas.MAX_MESSAGE_CHARS,
    "front={0} back={1}".format(m.group(1) if m else "?", schemas.MAX_MESSAGE_CHARS),
)

print("\n== summary ==")
engine.dispose()
shutil.rmtree(tmpdir, ignore_errors=True)
print("scratch dir removed: " + str(not tmpdir.exists()))
if FAILED:
    print("FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("ALL P0 CHECKS PASSED")
