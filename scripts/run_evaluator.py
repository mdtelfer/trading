from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import os
import time
from typing import Any, cast

import psycopg2
from psycopg2.extensions import connection as PGConnection
from psycopg2.extras import Json
from src.log import get_logger, init_logger
import yaml

"""
Macro Evaluator (fast/slow → fused)
-----------------------------------
- Lee reglas desde YAML (configs/fundamental_rules.yaml por defecto)
- Toma snapshot de features desde core.v_macro_latest
- Evalúa: hard_blocks → risk_bands → scenarios → overrides
- Inserta resultados en core.macro_state (tier: fast, slow) y uno fusionado (fused)
"""

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
init_logger()
logger = get_logger("macro_evaluator")


# ---------------------------------------------------------------------------
# Helpers de tipado/seguridad
# ---------------------------------------------------------------------------


def try_float(x: Any) -> float | None:
    """Convierte a float si puede, o devuelve None sin lanzar excepción."""
    try:
        return float(x) if x is not None else None
    except Exception:
        return None


def as_str_list(xs: Any) -> list[str]:
    """Filtra a list[str], ignora None u objetos no str."""
    if not isinstance(xs, list | tuple):
        return []
    return [s for s in xs if isinstance(s, str)]


def now_utc() -> datetime:
    return datetime.now(UTC)


def env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v is not None else default


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Carga YAML y helpers
# ---------------------------------------------------------------------------


@dataclass
class Rules:
    raw: dict[str, Any]
    portfolio_groups: dict[str, Any]
    eval_cfg: dict[str, Any]
    fuse_cfg: dict[str, Any]
    hard_blocks: list[dict[str, Any]]
    risk_bands: list[dict[str, Any]]
    scenarios: list[dict[str, Any]]
    overrides: dict[str, Any]


def load_rules(path: str) -> Rules:
    logger.info("Cargando reglas: %s", path)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    rules = Rules(
        raw=cast(dict[str, Any], data),
        portfolio_groups=cast(dict[str, Any], data.get("portfolio", {}).get("groups", {})),
        eval_cfg=cast(dict[str, Any], data.get("evaluation", {})),
        fuse_cfg=cast(dict[str, Any], data.get("fuse", {})),
        hard_blocks=cast(list[dict[str, Any]], data.get("hard_blocks", [])),
        risk_bands=cast(list[dict[str, Any]], data.get("risk_bands", [])),
        scenarios=cast(list[dict[str, Any]], data.get("scenarios", [])),
        overrides=cast(dict[str, Any], data.get("symbol_overrides", {})),
    )
    logger.info(
        "Reglas cargadas | groups=%d hard_blocks=%d risk_bands=%d scenarios=%d",
        len(rules.portfolio_groups),
        len(rules.hard_blocks),
        len(rules.risk_bands),
        len(rules.scenarios),
    )
    return rules


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


@dataclass
class DBCreds:
    host: str
    port: int
    dbname: str
    user: str
    password: str


def load_db_creds() -> DBCreds:
    return DBCreds(
        host=env_str("DB_HOST", "localhost"),
        port=env_int("DB_PORT", 5432),
        dbname=env_str("DB_NAME", "trading"),
        user=env_str("DB_USER", "postgres"),
        password=env_str("DB_PASSWORD", "postgres"),
    )


def get_latest_features(conn: PGConnection) -> list[dict[str, Any]]:
    """Lee snapshot simple de core.v_macro_latest."""
    sql = """
        SELECT feature, value, ts_utc, aux_values, status
        FROM core.v_macro_latest
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for feature, value, ts, aux, status in rows:
        out.append(
            {
                "feature": cast(str, feature),
                "value": try_float(value),
                "ts": ts,
                "aux": cast(dict[str, Any], aux or {}),
                "status": cast(str, status),
            }
        )
    return out


def insert_macro_state(
    conn: PGConnection,
    *,
    tier: str,
    ts: datetime,
    long_permission: bool,
    risk_multiplier: float,
    allowed_groups: list[str],
    prioritize: list[str],
    avoid: list[str],
    reason: str | None,
    meta: dict[str, Any],
) -> None:
    sql = """
        INSERT INTO core.macro_state (
            ts, tier, long_permission, risk_multiplier,
            allowed_groups, prioritize, avoid, reason, meta
        ) VALUES (
            %(ts)s, %(tier)s, %(lp)s, %(rm)s,
            %(ag)s, %(prio)s, %(avoid)s, %(reason)s, %(meta)s
        )
    """
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(
            sql,
            dict(
                ts=ts,
                tier=tier,
                lp=long_permission,
                rm=risk_multiplier,
                ag=allowed_groups,
                prio=prioritize,
                avoid=avoid,
                reason=reason,
                meta=Json(meta),
            ),
        )
    conn.commit()
    ms = int((time.time() - t0) * 1000)
    hb_txt = "+".join(meta.get("hard_blocks", [])) if meta else ""
    logger.info(
        ("Insert macro_state | tier=%s rm=%.2f groups=%s " "prio=%d avoid=%d hb=%s | ms=%d"),
        tier,
        risk_multiplier,
        ",".join(allowed_groups),
        len(prioritize),
        len(avoid),
        hb_txt,
        ms,
    )


# ---------------------------------------------------------------------------
# Normalización de valores (unidades)
# ---------------------------------------------------------------------------


def normalize_value(feature: str, value: float | None, aux: Mapping[str, Any]) -> float | None:
    if value is None:
        return None

    # 1) si viene value_pct en aux, úsalo
    vp = aux.get("value_pct")
    if vp is not None:
        v = try_float(vp)
        if v is not None:
            return v

    # 2) si aux.unit dice stored=decimal y original=percent => *100
    unit_raw = aux.get("unit")
    if isinstance(unit_raw, Mapping):
        unit_map: Mapping[str, Any] = cast(Mapping[str, Any], unit_raw)
        stored = unit_map.get("stored")
        original = unit_map.get("original")
        if stored == "decimal" and original == "percent":
            return float(value) * 100.0

    # 3) valor tal cual
    return float(value)


# ---------------------------------------------------------------------------
# Evaluación de condiciones y escenarios
# ---------------------------------------------------------------------------

_OPS: dict[str, Callable[[float, float], bool]] = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def check_condition(
    cond: Mapping[str, Any], snapshot: Mapping[str, float]
) -> tuple[bool, str | None]:
    """Devuelve (ok, missing_feature). snapshot tiene valores normalizados."""
    feat = cast(str | None, cond.get("feature"))
    if feat is None or feat not in snapshot:
        return False, feat
    left = snapshot.get(feat)
    if left is None:
        return False, feat
    op = cast(str, cond.get("op", "=="))
    right_raw = cond.get("value")
    right = try_float(right_raw)
    if right is None:
        return False, None
    fn = _OPS.get(op)
    if fn is None:
        return False, None
    try:
        return fn(float(left), float(right)), None
    except Exception:
        return False, None


def eval_clause_ex(
    clause: Mapping[str, Any], snapshot: Mapping[str, float]
) -> tuple[bool, list[str], int, int, str]:
    """
    Evalúa 'all'/'any' y devuelve:
      (resultado_bool, faltantes, true_count, total_count, mode)
    """
    missing: list[str] = []

    all_cond = clause.get("all")
    if isinstance(all_cond, list):
        oks_all: list[bool] = []
        for c in all_cond:
            ok, miss = check_condition(c, snapshot)
            if miss:
                missing.append(miss)
            oks_all.append(ok)
        true_count = sum(1 for x in oks_all if x)
        return all(oks_all), missing, true_count, len(all_cond), "all"

    any_cond = clause.get("any")
    if isinstance(any_cond, list):
        oks_any: list[bool] = []
        for c in any_cond:
            ok, miss = check_condition(c, snapshot)
            if miss:
                missing.append(miss)
            oks_any.append(ok)
        true_count = sum(1 for x in oks_any if x)
        return any(oks_any), missing, true_count, len(any_cond), "any"

    return False, missing, 0, 0, "none"


def eval_confirmations(
    conf_obj: Any, snapshot: Mapping[str, float]
) -> tuple[bool, list[str], list[dict[str, Any]]]:
    """
    Soporta:
      - dict con 'all'/'any' (un solo bloque)
      - lista de bloques [{'all': [...]}, {'any': [...]}]
    Devuelve: (ok_global, faltantes_acumulados, detalles_por_bloque)
    """
    if conf_obj is None:
        return True, [], []

    blocks = conf_obj if isinstance(conf_obj, list) else [conf_obj]
    blocks = cast(list[Mapping[str, Any]], blocks)

    all_missing: list[str] = []
    details: list[dict[str, Any]] = []
    ok_all = True

    for blk in blocks:
        ok, miss, true_cnt, total_cnt, mode = eval_clause_ex(blk, snapshot)
        all_missing.extend(miss)
        details.append({"mode": mode, "true": true_cnt, "total": total_cnt, "ok": ok})
        ok_all = ok_all and ok

    return ok_all, list(sorted(set(all_missing))), details


@dataclass
class EvalResult:
    tier: str
    ts: datetime
    long_permission: bool
    risk_multiplier: float
    allowed_groups: list[str]
    prioritize: list[str]
    avoid: list[str]
    reason: str | None
    meta: dict[str, Any]


def unique(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_buy_suggestions(
    active_scenarios: list[str],
    rules: Rules,
    avoids: list[str],
) -> list[dict[str, Any]]:
    votes: dict[str, int] = defaultdict(int)
    overrides: dict[str, dict[str, Any]] = {}

    for sc in rules.scenarios:
        name = cast(str, sc.get("name"))
        if name in active_scenarios:
            eff = cast(dict[str, Any], sc.get("effect", {}) or {})
            for s in as_str_list(eff.get("prioritize_symbols", [])):
                votes[s] += 1
            pov = cast(dict[str, Any], eff.get("priority_overrides") or {})
            if isinstance(pov, dict):
                overrides.update(pov)

    def group_of(sym: str) -> str:
        for g, cfg in rules.portfolio_groups.items():
            if sym in (cfg or {}).get("symbols", []):  # pyright: ignore[reportUnknownMemberType]
                return g
        return "CORE"

    suggestions: list[dict[str, Any]] = []
    for sym, cnt in votes.items():
        base = min(cnt, 2) * 0.35  # máx 0.70
        grp = group_of(sym)
        group_boost = 0.15 if grp == "CORE" else (0.05 if grp == "EXTENSION" else 0.0)
        penalty = -0.30 if sym in avoids else 0.0
        score = base + group_boost + penalty

        ov = overrides.get(sym) or {}
        bump = try_float(ov.get("bump", 0.0)) or 0.0
        score = max(0.0, min(1.0, score + bump))
        tags = as_str_list(ov.get("tags", []))

        suggestions.append(
            {
                "symbol": sym,
                "group": grp,
                "score": round(score, 2),
                "reasons": active_scenarios,
                "tags": tags,
            }
        )

    suggestions.sort(key=lambda x: x["score"], reverse=True)
    return suggestions


def evaluate_tier(  # noqa: C901
    tier: str,
    snapshot_rows: list[dict[str, Any]],
    rules: Rules,
) -> EvalResult:
    # Normalizar snapshot → feature -> value(float)
    snap: dict[str, float] = {}
    for row in snapshot_rows:
        feature = cast(str, row["feature"])
        val = normalize_value(feature, row.get("value"), row.get("aux", {}))
        if val is not None:
            snap[feature] = float(val)

    ts = now_utc()
    block_is_terminal = bool(rules.eval_cfg.get("block_is_terminal", True))
    min_true = int(rules.eval_cfg.get("min_true_per_scenario", 2))

    # defaults
    long_permission = True  # long-only global (no se usa para gating ahora)
    risk_multiplier = 1.0
    allowed_groups = list(rules.portfolio_groups.keys()) or ["CORE"]
    prioritize: list[str] = []
    avoid: list[str] = []
    reason: str | None = None

    hard_hits: list[str] = []
    missing_overall: list[str] = []
    can_open_new = True
    blocked_by: str | None = None
    block_exclusions: list[str] = []

    # 1) hard blocks
    for hb in rules.hard_blocks:
        name = cast(str, hb.get("name", "hard_block"))
        ok, miss, _, _, _ = eval_clause_ex(hb, snap)
        if miss:
            missing_overall.extend(miss)
        if ok:
            hard_hits.append(name)
            eff = cast(dict[str, Any], hb.get("effect", {}) or {})
            if eff.get("block_new_entries"):
                can_open_new = False
                blocked_by = name
            elif "block_new_entries_excluding" in eff:
                can_open_new = False
                blocked_by = name
                block_exclusions = as_str_list(eff.get("block_new_entries_excluding", []))
            rm_hb = try_float(eff.get("risk_multiplier_macro"))
            if rm_hb is not None:
                risk_multiplier = min(risk_multiplier, rm_hb)
            caps_to = eff.get("cap_groups_to")
            if isinstance(caps_to, list | tuple):
                caps_list = as_str_list(caps_to)
                allowed_groups = [g for g in allowed_groups if g in caps_list]
            if block_is_terminal and (not can_open_new):
                break

    # 2) risk bands (conservador: tomar el menor multiplicador entre coincidencias)
    band_name: str | None = None
    for rb in rules.risk_bands:
        ok, miss, _, _, _ = eval_clause_ex(rb, snap)
        if miss:
            missing_overall.extend(miss)
        if ok:
            eff = cast(dict[str, Any], rb.get("effect", {}) or {})
            rm = try_float(eff.get("risk_multiplier_macro", risk_multiplier))
            if rm is not None and rm < risk_multiplier:
                risk_multiplier = rm
                band_name = cast(str | None, rb.get("name"))

    # ---- acumulador de caps por grupo (no filtra elegibilidad) ----
    group_caps_acc: dict[str, dict[str, int]] = {}

    def _merge_group_caps(dst: dict[str, dict[str, int]], src: Mapping[str, Any] | None) -> None:
        if not isinstance(src, Mapping):
            return
        for g, cfg in src.items():
            if not isinstance(cfg, Mapping):
                continue
            m = try_float(cfg.get("max_open_positions"))  # pyright: ignore[reportUnknownMemberType]
            m_int = int(m) if m is not None else None
            if m_int is None:
                continue
            if g in dst:
                dst[g]["max_open_positions"] = min(dst[g]["max_open_positions"], m_int)
            else:
                dst[g] = {"max_open_positions": m_int}

    # 3) scenarios
    active_scenarios: list[str] = []
    for sc_item in rules.scenarios:
        sc: dict[str, Any] = sc_item
        name = cast(str, sc.get("name"))

        ok, miss, true_cnt, total_cnt, mode = eval_clause_ex(sc, snap)
        if miss:
            missing_overall.extend(miss)

        if mode == "all":
            activate = ok
        elif mode == "any":
            needed = min(min_true, total_cnt if total_cnt > 0 else 1)
            activate = true_cnt >= needed
        else:
            activate = False

        logger.debug(
            "%s scen=%s mode=%s true=%d/%d min_true=%d pre_conf=%s",
            tier.upper(),
            name,
            mode,
            true_cnt,
            total_cnt,
            min_true,
            activate,
        )

        if activate:
            conf = cast(dict[str, Any] | list[dict[str, Any]] | None, sc.get("confirmations"))
            conf_ok, conf_missing, _ = eval_confirmations(conf, snap)
            if conf_missing:
                missing_overall.extend(conf_missing)
            activate = activate and conf_ok
            logger.debug(
                "%s scen=%s confirmations -> ok=%s missing=%s final=%s",
                tier.upper(),
                name,
                conf_ok,
                conf_missing if conf_missing else "[]",
                activate,
            )

        if activate:
            active_scenarios.append(name)
            eff: dict[str, Any] = cast(dict[str, Any], sc.get("effect", {}) or {})

            if eff.get("allow_new_entries"):
                can_open_new = can_open_new and True

            if "allowed_groups" in eff:
                g = as_str_list(eff.get("allowed_groups"))
                if g:
                    allowed_groups = [x for x in allowed_groups if x in g]

            if "group_caps" in eff:
                _merge_group_caps(
                    group_caps_acc, cast(Mapping[str, Any] | None, eff.get("group_caps"))
                )

            prioritize.extend(as_str_list(eff.get("prioritize_symbols", [])))
            avoid.extend(as_str_list(eff.get("avoid_symbols", [])))

    prioritize = unique(prioritize)
    avoid = unique(avoid)

    # 4) buy suggestions para UI
    suggestions = build_buy_suggestions(active_scenarios, rules, avoid)

    meta: dict[str, Any] = {
        "hard_blocks": hard_hits,
        "risk_band": band_name or "normal",
        "triggered_scenarios": active_scenarios,
        "can_open_new": bool(can_open_new),
        "blocked_by": blocked_by,
        "block_exclusions": block_exclusions,
        "buy_suggestions": suggestions,
        "missing_features": sorted(unique(missing_overall)),
        "feature_sample_size": len(snap),
        "group_caps": group_caps_acc,
    }

    reason = reason or f"band={meta['risk_band']} hb={'+'.join(hard_hits) if hard_hits else 'none'}"

    return EvalResult(
        tier=tier,
        ts=ts,
        long_permission=long_permission,
        risk_multiplier=float(risk_multiplier),
        allowed_groups=allowed_groups,
        prioritize=prioritize,
        avoid=avoid,
        reason=reason,
        meta=meta,
    )


def fuse_states(fast: EvalResult, slow: EvalResult, rules: Rules) -> EvalResult:  # noqa: C901
    cfg = cast(dict[str, Any], rules.fuse_cfg or {})
    allow_logic = cast(str, cfg.get("allow_logic", "fast_and_slow"))
    risk_logic = cast(str, cfg.get("risk_logic", "min"))
    prioritize_logic = cast(str, cfg.get("prioritize_logic", "intersection"))
    avoid_logic = cast(str, cfg.get("avoid_logic", "union"))
    groups_logic = cast(str, cfg.get("groups_logic", "intersection"))

    def inter(a: list[str], b: list[str]) -> list[str]:
        bset = set(b)
        return [x for x in a if x in bset]

    def uni(a: list[str], b: list[str]) -> list[str]:
        return unique(a + b)

    # can_open_new
    if allow_logic == "fast_and_slow":
        can_open_new = bool(fast.meta.get("can_open_new")) and bool(slow.meta.get("can_open_new"))
    elif allow_logic == "fast_or_slow":
        can_open_new = bool(fast.meta.get("can_open_new")) or bool(slow.meta.get("can_open_new"))
    else:
        can_open_new = False

    # risk multiplier
    if risk_logic == "min":
        rm = min(fast.risk_multiplier, slow.risk_multiplier)
    elif risk_logic == "max":
        rm = max(fast.risk_multiplier, slow.risk_multiplier)
    else:
        rm = min(fast.risk_multiplier, slow.risk_multiplier)

    # groups
    allowed_groups = (
        inter(fast.allowed_groups, slow.allowed_groups)
        if groups_logic == "intersection"
        else uni(fast.allowed_groups, slow.allowed_groups)
    )

    # prioritize & avoid
    prioritize = (
        inter(fast.prioritize, slow.prioritize)
        if prioritize_logic == "intersection"
        else uni(fast.prioritize, slow.prioritize)
    )
    avoid = uni(fast.avoid, slow.avoid) if avoid_logic == "union" else inter(fast.avoid, slow.avoid)

    # merge group_caps conservador (min por grupo)
    def _merge_caps(a: Any, b: Any) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}

        def _add(src: Any) -> None:
            if not isinstance(src, dict):
                return
            for g, cfg_g in src.items():
                if not isinstance(cfg_g, dict):
                    continue
                m = try_float(
                    cfg_g.get("max_open_positions")
                )  # pyright: ignore[reportUnknownMemberType]
                m_int = int(m) if m is not None else None
                if m_int is None:
                    continue
                if g in out:
                    out[g]["max_open_positions"] = min(out[g]["max_open_positions"], m_int)
                else:
                    out[g] = {"max_open_positions": m_int}

        _add(a)
        _add(b)
        return out

    fused_caps = _merge_caps(fast.meta.get("group_caps"), slow.meta.get("group_caps"))

    # buy suggestions (intersección + promedio score)
    f_sug = {
        s["symbol"]: s for s in cast(list[dict[str, Any]], fast.meta.get("buy_suggestions", []))
    }
    s_sug = {
        s["symbol"]: s for s in cast(list[dict[str, Any]], slow.meta.get("buy_suggestions", []))
    }
    fused_syms = set(f_sug.keys()) & set(s_sug.keys())
    buy_suggestions: list[dict[str, Any]] = []
    for sym in fused_syms:
        fs = f_sug[sym]
        ss = s_sug[sym]
        fs_score = try_float(fs.get("score")) or 0.0
        ss_score = try_float(ss.get("score")) or 0.0
        buy_suggestions.append(
            {
                "symbol": sym,
                "group": fs.get("group") or ss.get("group"),
                "score": round((fs_score + ss_score) / 2.0, 2),
                "reasons": unique(as_str_list(fs.get("reasons")) + as_str_list(ss.get("reasons"))),
                "tags": unique(as_str_list(fs.get("tags")) + as_str_list(ss.get("tags"))),
            }
        )
    buy_suggestions.sort(key=lambda x: x["score"], reverse=True)

    meta: dict[str, Any] = {
        "hard_blocks": unique(
            as_str_list(fast.meta.get("hard_blocks")) + as_str_list(slow.meta.get("hard_blocks"))
        ),
        "risk_band": f"fast={fast.meta.get('risk_band')} | slow={slow.meta.get('risk_band')}",
        "triggered_scenarios": unique(
            as_str_list(fast.meta.get("triggered_scenarios"))
            + as_str_list(slow.meta.get("triggered_scenarios"))
        ),
        "can_open_new": bool(can_open_new),
        "blocked_by": slow.meta.get("blocked_by") or fast.meta.get("blocked_by"),
        "buy_suggestions": buy_suggestions,
        "fuse": rules.fuse_cfg,
        "group_caps": fused_caps,
    }

    return EvalResult(
        tier="fused",
        ts=now_utc(),
        long_permission=True,
        risk_multiplier=float(rm),
        allowed_groups=allowed_groups,
        prioritize=prioritize,
        avoid=avoid,
        reason=f"fused allow={allow_logic} risk={risk_logic}",
        meta=meta,
    )


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------


def run_once(conn: PGConnection, rules: Rules, tier: str) -> EvalResult:
    rows = get_latest_features(conn)
    return evaluate_tier(tier=tier, snapshot_rows=rows, rules=rules)


def main() -> None:
    creds = load_db_creds()
    rules_path = env_str("RULES_PATH", "configs/fundamental_rules.yaml")
    rules = load_rules(rules_path)

    fast_every = env_int("FAST_EVERY_SEC", 300)
    slow_every = env_int("SLOW_EVERY_SEC", 3600)

    logger.info(
        "Arrancando evaluator | DB=%s:%d/%s fast=%ds slow=%ds rules=%s",
        creds.host,
        creds.port,
        creds.dbname,
        fast_every,
        slow_every,
        rules_path,
    )

    next_fast = now_utc()
    next_slow = now_utc()
    fast_res: EvalResult | None = None

    while True:
        did_work = False
        try:
            with closing(psycopg2.connect(**asdict(creds))) as conn:
                now = now_utc()

                # FAST
                if now >= next_fast:
                    t0 = time.time()
                    fast_res = run_once(conn, rules, tier="fast")
                    ms = int((time.time() - t0) * 1000)
                    logger.info(
                        (
                            "Eval fast | can_open_new=%s rm=%.2f band=%s hb=%s "
                            "scen=%d groups=%s missing=%d snap=%d | ms=%d"
                        ),
                        fast_res.meta.get("can_open_new"),
                        fast_res.risk_multiplier,
                        fast_res.meta.get("risk_band"),
                        "+".join(as_str_list(fast_res.meta.get("hard_blocks"))) or "none",
                        len(as_str_list(fast_res.meta.get("triggered_scenarios"))),
                        ",".join(fast_res.allowed_groups),
                        len(as_str_list(fast_res.meta.get("missing_features"))),
                        int(fast_res.meta.get("feature_sample_size", 0)),
                        ms,
                    )
                    insert_macro_state(conn, **fast_res.__dict__)
                    did_work = True
                    next_fast = now + timedelta(seconds=fast_every)

                # SLOW (+ FUSED)
                if now >= next_slow:
                    t0 = time.time()
                    slow_res = run_once(conn, rules, tier="slow")
                    ms = int((time.time() - t0) * 1000)
                    logger.info(
                        (
                            "Eval slow | can_open_new=%s rm=%.2f band=%s hb=%s "
                            "scen=%d groups=%s missing=%d snap=%d | ms=%d"
                        ),
                        slow_res.meta.get("can_open_new"),
                        slow_res.risk_multiplier,
                        slow_res.meta.get("risk_band"),
                        "+".join(as_str_list(slow_res.meta.get("hard_blocks"))) or "none",
                        len(as_str_list(slow_res.meta.get("triggered_scenarios"))),
                        ",".join(slow_res.allowed_groups),
                        len(as_str_list(slow_res.meta.get("missing_features"))),
                        int(slow_res.meta.get("feature_sample_size", 0)),
                        ms,
                    )
                    insert_macro_state(conn, **slow_res.__dict__)

                    if fast_res is None:
                        fast_res = slow_res  # fallback defensivo
                    fused = fuse_states(fast_res, slow_res, rules)
                    allow_logic_log = cast(str, rules.fuse_cfg.get("allow_logic", "fast_and_slow"))
                    risk_logic_log = cast(str, rules.fuse_cfg.get("risk_logic", "min"))

                    logger.info(
                        (
                            "FUSE | allow=%s risk=%s -> can_open_new=%s rm=%.2f "
                            "groups=%s prio=%d avoid=%d"
                        ),
                        allow_logic_log,
                        risk_logic_log,
                        fused.meta.get("can_open_new"),
                        fused.risk_multiplier,
                        ",".join(fused.allowed_groups),
                        len(fused.prioritize),
                        len(fused.avoid),
                    )

                    insert_macro_state(conn, **fused.__dict__)
                    did_work = True
                    next_slow = now + timedelta(seconds=slow_every)

        except Exception as e:  # pragma: no cover - robustez en runtime
            logger.error("[evaluator] error: %s", e)

        if not did_work:
            time.sleep(1)


if __name__ == "__main__":
    main()
