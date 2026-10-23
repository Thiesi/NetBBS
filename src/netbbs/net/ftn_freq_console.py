"""
The SysOp console's file-request pieces (design doc §6.8, "File requests";
issue #1475), used by `netbbs.net.ftn_console` and the file area screen:

- **[R]equest files** on the FTN status screen: a draft with the node to
  ask (the uplink by default, or a node the nodelist lists) and the files,
  sent with [S]end. What comes back waits in Held items.
- **The File requests section** of the network editor: answering (off by
  default), who may request, the limits, and the ABOUT and NODELIST files.
- **The status rows**: whether the network answers, and its recent
  requests, made and answered.
- **[O]ffer to file requests** on a file area's screen, a toggle.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import parse_address
from netbbs.ftn.freq import (
    DAY_FILES_RANGE,
    DAY_MIB_RANGE,
    MIB,
    SESSION_FILES_RANGE,
    SESSION_MIB_RANGE,
    FreqError,
    FreqSettings,
    get_settings,
    is_area_offered,
    offer_problem,
    queue_request,
    recent_requests,
    request_lines_from_text,
    request_state,
    save_settings_without_commit,
    servable_areas,
    set_area_offered,
)
from netbbs.ftn.networks import FtnNetwork, get_network
from netbbs.ftn.queue import FtnQueueFullError
from netbbs.moderation.log import record_action
from netbbs.net.admin_fields import _int_field, _optional_text_field
from netbbs.net.admin_ui import _announce, _announce_line, _load_chrome, edit_resource_draft
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.resource_editor import FieldSpec, bool_field, bool_step, text_field
from netbbs.net.session import Session
from netbbs.rendering.detail import Field
from netbbs.rendering.menu import menu_key
from netbbs.rendering.sanitize import sanitize_text
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

# --- [R]equest files ---------------------------------------------------------------


async def request_files(session: Session, lane: DatabaseLane, actor: User, network: FtnNetwork, mailer) -> None:
    """The request draft: nothing is written before [S]end."""
    draft = {"node": network.uplink_address.four_d, "files": ""}
    fields = [
        FieldSpec(key="node", label="From node",
                  render=lambda d: d["node"] or "(not set)", prompt=text_field("node", required=True),
                  brief="The uplink, or a node the nodelist lists as answering BinkP",
                  help="A request goes straight to that node, at once. Your hub is the usual one to ask."),
        FieldSpec(key="files", label="Files",
                  render=lambda d: d["files"] or "(none yet)", prompt=text_field("files", required=True),
                  brief="File names, * and ? patterns, or FILES, ABOUT, NODELIST",
                  help="Separate names with spaces. FILES asks for the node's list of files, ABOUT for its "
                       "description, NODELIST for the network's nodelist. A password the node gave you "
                       "follows its file: THIS.ZIP !secret."),
    ]

    async def save(draft: dict):
        try:
            node = parse_address(draft["node"])
        except FtnFormatError as exc:
            raise FreqError(f"{exc}.") from None
        lines = request_lines_from_text(draft["files"])

        def _persist(db: Database):
            current = get_network(db, network.id)
            if current is None:
                raise FreqError("That network no longer exists.")
            try:
                record = queue_request(db, current, node, lines)
            except FtnQueueFullError as exc:
                raise FreqError(str(exc)) from None
            record_action(db, actor=actor, action="ftn_file_request",
                          detail=f"{current.name}: requested {record.names} from {record.node}")
            return record

        return await lane.run(_persist)

    chrome = await _load_chrome(lane, actor)
    record = await edit_resource_draft(
        session, lane, title=f"Request files ({network.name})", fields=fields, draft=draft, save=save,
        error_type=FreqError, save_menu_text=menu_key("S", "end"), back_menu_text=menu_key("B", "ack"),
        save_brief="Queue the request; it goes out at once",
        preamble="What comes back waits in Held items, from where you save it to a file area.",
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
        accent_color=chrome.accent_color, header_color=chrome.header_color,
    )
    if record is None:
        return
    if mailer is not None:
        mailer.kick()
    _announce_line(session, f"File request to {record.node} queued: {record.names}. It goes out at once; what "
                            f"comes back waits in Held items.")


# --- the network editor's File requests section ---------------------------------------


def freq_draft(db: Database, network: FtnNetwork | None) -> dict:
    settings = get_settings(db, network.id) if network is not None and network.id is not None else FreqSettings(0)
    return {"freq_answer": settings.answer, "freq_secure_only": settings.secure_only,
            "freq_session_files": settings.session_files, "freq_session_mib": settings.session_bytes // MIB,
            "freq_day_files": settings.day_files, "freq_day_mib": settings.day_bytes // MIB,
            "freq_about_path": settings.about_path, "freq_serve_nodelist": settings.serve_nodelist,
            "freq_nodelist_path": settings.nodelist_path}


def freq_settings_from_draft(draft: dict, network_id: int) -> FreqSettings:
    """The draft's settings, checked; raises `FreqError` (a
    `ValueError`) with the reason."""
    return FreqSettings(
        network_id=network_id, answer=bool(draft["freq_answer"]), secure_only=bool(draft["freq_secure_only"]),
        session_files=int(draft["freq_session_files"]), session_bytes=int(draft["freq_session_mib"]) * MIB,
        day_files=int(draft["freq_day_files"]), day_bytes=int(draft["freq_day_mib"]) * MIB,
        about_path=draft["freq_about_path"], serve_nodelist=bool(draft["freq_serve_nodelist"]),
        nodelist_path=draft["freq_nodelist_path"],
    )


def save_freq_settings_without_commit(db: Database, draft: dict, network_id: int) -> None:
    save_settings_without_commit(db, freq_settings_from_draft(draft, network_id))


def freq_fields() -> list[FieldSpec]:
    section = "File requests"
    return [
        FieldSpec(key="freq_answer", label="Answer requests",
                  render=lambda d: "yes" if d["freq_answer"] else "no", prompt=bool_field("freq_answer"),
                  step=bool_step("freq_answer"), section=section,
                  brief="Send files other nodes ask for (FREQ)",
                  help="Off by default. On, a node asking in a binkp/1.1 session gets the files of the areas "
                       "you offer (a file area's [O]ffer to file requests), FILES (a list of them), ABOUT and "
                       "NODELIST, in the same call."),
        FieldSpec(key="freq_secure_only", label="Who may ask",
                  render=lambda d: "secure links only" if d["freq_secure_only"] else "anyone",
                  prompt=bool_field("freq_secure_only"), step=bool_step("freq_secure_only"), section=section,
                  brief="Secure links only (a session password), or any node",
                  help="A secure link proves itself with its session password: your hub, today. Anyone can "
                       "claim any address, so with anyone the limits are all that hold."),
        FieldSpec(key="freq_session_files", label="Files per call",
                  render=lambda d: str(d["freq_session_files"]),
                  prompt=_int_field("freq_session_files", "Files per call"), section=section,
                  brief=f"The most files one call gets ({SESSION_FILES_RANGE[0]}-{SESSION_FILES_RANGE[1]})"),
        FieldSpec(key="freq_session_mib", label="MiB per call",
                  render=lambda d: f"{d['freq_session_mib']} MiB",
                  prompt=_int_field("freq_session_mib", "MiB per call"), section=section,
                  brief=f"The most one call gets ({SESSION_MIB_RANGE[0]}-{SESSION_MIB_RANGE[1]} MiB)"),
        FieldSpec(key="freq_day_files", label="Files per node a day",
                  render=lambda d: str(d["freq_day_files"]),
                  prompt=_int_field("freq_day_files", "Files per node a day"), section=section,
                  brief=f"The most files one node gets in a day ({DAY_FILES_RANGE[0]}-{DAY_FILES_RANGE[1]})"),
        FieldSpec(key="freq_day_mib", label="MiB per node a day",
                  render=lambda d: f"{d['freq_day_mib']} MiB",
                  prompt=_int_field("freq_day_mib", "MiB per node a day"), section=section,
                  brief=f"The most one node gets in a day ({DAY_MIB_RANGE[0]}-{DAY_MIB_RANGE[1]} MiB)"),
        FieldSpec(key="freq_about_path", label="ABOUT file",
                  render=lambda d: d["freq_about_path"] or "(none: ABOUT is not served)",
                  prompt=_optional_text_field("freq_about_path"), section=section,
                  brief="The file a request for ABOUT gets, on this machine"),
        FieldSpec(key="freq_serve_nodelist", label="Serve NODELIST",
                  render=lambda d: "yes" if d["freq_serve_nodelist"] else "no",
                  prompt=bool_field("freq_serve_nodelist"), step=bool_step("freq_serve_nodelist"),
                  section=section, brief="Answer a request for NODELIST"),
        FieldSpec(key="freq_nodelist_path", label="NODELIST file",
                  render=lambda d: d["freq_nodelist_path"] or "(the nodelist as imported here)",
                  prompt=_optional_text_field("freq_nodelist_path"), section=section,
                  brief="The file NODELIST gets; empty for the network's nodelist as imported"),
    ]


# --- the status screen's rows -------------------------------------------------------------


def freq_status_rows(db: Database, network: FtnNetwork) -> list[Field]:
    settings = get_settings(db, network.id)
    if settings.answer:
        areas = servable_areas(db, network)
        state = (f"on, {'secure links only' if settings.secure_only else 'anyone'}; "
                 f"{len(areas)} file areas offered")
    else:
        state = "off"
    rows = [Field("File requests", state)]
    for record in recent_requests(db, network.id):
        if record.direction == "out":
            text = f"to {record.node}: {record.names} -- {request_state(db, record)}"
        else:
            text = f"from {record.node}: {record.names} -- {record.outcome}"
        rows.append(Field(record.created_at[:16].replace("T", " "), sanitize_text(text)))
    return rows


# --- a file area's [O]ffer to file requests ------------------------------------------------


def area_offer_rows(db: Database, area) -> list[Field]:
    if is_area_offered(db, area):
        problem = offer_problem(db, area)
        return [Field("File requests", "offered" if problem is None else f"offered, but not served: {problem}")]
    return [Field("File requests", "not offered")]


def area_offer_label(offered: bool):
    return menu_key("O", "ffer to file requests: " + ("yes" if offered else "no"))


async def toggle_area_offer(session: Session, lane: DatabaseLane, actor: User, area) -> bool:
    """[O]ffer to file requests: on or off. Returns whether it changed."""
    offered = await lane.run(is_area_offered, area)

    def _persist(db: Database) -> str:
        set_area_offered(db, area, not offered)
        outcome = (f"{area.name} is no longer offered to file requests." if offered else
                   f"{area.name} is offered to file requests, on every network that answers them.")
        record_action(db, actor=actor, action="set_file_area_freq", detail=outcome)
        return outcome

    try:
        outcome = await lane.run(_persist)
    except FreqError as exc:
        _announce(session, str(exc), error=True)
        return False
    _announce(session, outcome)
    return True
