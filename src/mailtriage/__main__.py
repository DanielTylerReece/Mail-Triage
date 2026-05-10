"""Entry point: webhook server + N parallel workers + scheduler."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import categories as cats
from . import classifier as classifier_mod
from . import rules as rules_mod
from .config import get_settings, load_mailboxes
from .dispatcher import CrossMailboxError, Dispatcher, UnknownCategoryError
from .graph import GraphClient, GraphError, Mailbox
from .queue import FairQueue, Job
from .storage import Decision, SqliteStorage, now_iso
from .subscriptions import renew as renew_subscriptions
from .webhook import make_app

log = logging.getLogger("mailtriage.lifecycle")
worker_log = logging.getLogger("mailtriage.worker")


# ---- shared health stats (read by /healthz) ----


_last_decision_ts: float = 0.0
_workers_alive = 0


def _health_snapshot(queue: FairQueue, dry_run: bool) -> dict:
    """Build the dict /healthz returns. Must not raise; webhook handles it
    if it does."""
    last = _last_decision_ts
    last_age = (time.time() - last) if last else None
    return {
        "queue_depth": queue.depth,
        "in_flight": queue.in_flight,
        "workers_alive": _workers_alive,
        "last_decision_age_seconds": last_age,
        "dry_run": dry_run,
    }


# -------- worker --------


async def worker(
    name: str,
    queue: FairQueue,
    graph: GraphClient,
    dispatcher: Dispatcher,
    storage: SqliteStorage,
    rules: list,
    mbx_index: dict[str, Mailbox],
    shutdown: asyncio.Event,
) -> None:
    global _workers_alive
    _workers_alive += 1
    try:
        while not shutdown.is_set():
            try:
                job = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                worker_log.exception("[%s] queue.get failed; will retry", name)
                await asyncio.sleep(1.0)
                continue

            try:
                await asyncio.to_thread(
                    _handle_one, job, graph, dispatcher, storage, rules, mbx_index,
                )
            except Exception:
                worker_log.exception("[%s] worker crashed on job %s/%s",
                                     name, job.user_id, job.message_id)
            finally:
                try:
                    await queue.done(job)
                except Exception:
                    worker_log.exception("[%s] queue.done failed for %s/%s",
                                         name, job.user_id, job.message_id)
    finally:
        _workers_alive -= 1


def _handle_one(
    job: Job,
    graph: GraphClient,
    dispatcher: Dispatcher,
    storage: SqliteStorage,
    rules: list,
    mbx_index: dict[str, Mailbox],
) -> None:
    global _last_decision_ts
    mbx = mbx_index.get(job.user_id)
    if mbx is None:
        worker_log.warning("dropping job for unconfigured user_id %s", job.user_id)
        return
    mailbox_addr = mbx.primary_upn

    if storage.has_processed(mailbox_addr, job.message_id):
        worker_log.debug("dedup: %s / %s already processed",
                         mailbox_addr, job.message_id)
        storage.record(Decision(
            timestamp=now_iso(), mailbox=mailbox_addr, message_id=job.message_id,
            sender=None, subject=None, source="dedup", rule=None,
            category="triage", confidence=None, reasoning="duplicate notification",
            action="skipped-duplicate", destination=None,
            provider=None, model=None, error=None,
        ))
        _last_decision_ts = time.time()
        return

    try:
        msg = graph.get_message(job.user_id, job.message_id)
    except GraphError as e:
        worker_log.error("could not fetch message %s/%s: %s",
                         mailbox_addr, job.message_id, e)
        storage.record(Decision(
            timestamp=now_iso(), mailbox=mailbox_addr, message_id=job.message_id,
            sender=None, subject=None, source="error", rule=None,
            category="triage", confidence=None, reasoning=None,
            action="aborted", destination=None,
            provider=None, model=None, error=str(e)[:200],
        ))
        _last_decision_ts = time.time()
        return

    sender = (((msg.get("from") or {}).get("emailAddress") or {}).get("address") or "")
    subject = msg.get("subject") or ""

    t1 = rules_mod.evaluate(rules, mailbox_addr, msg)
    if t1.matched:
        category = t1.category or "triage"
        source = "tier1"
        rule_repr = f"line {t1.rule.line_no}: {t1.rule.raw}" if t1.rule else None
        confidence: float | None = 1.0
        reasoning = f"matched rule: {t1.rule.match_type} {t1.rule.pattern}" if t1.rule else None
        provider = model = None
    else:
        t2 = classifier_mod.classify(msg, mailbox=mailbox_addr, message_id=job.message_id)
        category = t2.category
        source = "tier2"
        rule_repr = None
        confidence = t2.confidence
        reasoning = t2.reasoning
        provider, model = t2.provider, t2.model
        if t2.error:
            worker_log.warning("tier2 error for %s/%s: %s",
                               mailbox_addr, job.message_id, t2.error)

    try:
        action, dest = dispatcher.dispatch(
            user_id=job.user_id, message_id=job.message_id,
            message=msg, category=category,
        )
    except CrossMailboxError as e:
        worker_log.critical(
            "CROSS-MAILBOX VIOLATION blocked for %s msg=%s category=%s: %s",
            mailbox_addr, job.message_id, category, e,
        )
        storage.record(Decision(
            timestamp=now_iso(), mailbox=mailbox_addr, message_id=job.message_id,
            sender=sender, subject=subject, source="guard", rule=rule_repr,
            category=category, confidence=confidence, reasoning=reasoning,
            action="aborted-permanent", destination=None,
            provider=provider, model=model, error=str(e)[:200],
        ))
        _last_decision_ts = time.time()
        return
    except UnknownCategoryError as e:
        worker_log.error("unknown category %r for %s/%s; falling back to triage",
                         category, mailbox_addr, job.message_id)
        try:
            action, dest = dispatcher.dispatch(
                user_id=job.user_id, message_id=job.message_id,
                message=msg, category="triage",
            )
            category = "triage"
        except Exception as e2:
            worker_log.exception("triage fallback also failed for %s/%s",
                                 mailbox_addr, job.message_id)
            storage.record(Decision(
                timestamp=now_iso(), mailbox=mailbox_addr, message_id=job.message_id,
                sender=sender, subject=subject, source=source, rule=rule_repr,
                category=category, confidence=confidence, reasoning=reasoning,
                action="aborted", destination=None,
                provider=provider, model=model,
                error=f"{type(e).__name__}; fallback: {type(e2).__name__}",
            ))
            _last_decision_ts = time.time()
            return
    except Exception as e:
        worker_log.exception("dispatch failed for %s/%s",
                             mailbox_addr, job.message_id)
        storage.record(Decision(
            timestamp=now_iso(), mailbox=mailbox_addr, message_id=job.message_id,
            sender=sender, subject=subject, source=source, rule=rule_repr,
            category=category, confidence=confidence, reasoning=reasoning,
            action="aborted", destination=None,
            provider=provider, model=model, error=str(e)[:200],
        ))
        _last_decision_ts = time.time()
        return

    storage.record(Decision(
        timestamp=now_iso(), mailbox=mailbox_addr, message_id=job.message_id,
        sender=sender, subject=subject, source=source, rule=rule_repr,
        category=category, confidence=confidence, reasoning=reasoning,
        action=action, destination=dest,
        provider=provider, model=model, error=None,
    ))
    _last_decision_ts = time.time()
    log_prefix = "DRY-RUN: would " if action == "dry-run" else ""
    worker_log.info(
        "%sdecision mailbox=%s msg=%s source=%s category=%s action=%s dest=%s",
        log_prefix, mailbox_addr, job.message_id, source, category, action, dest,
    )


# -------- scheduler jobs --------


async def renewal_job() -> None:
    try:
        await asyncio.to_thread(renew_subscriptions)
    except Exception:
        log.exception("subscription renewal job failed")


async def retention_job(storage: SqliteStorage, retention_days: int) -> None:
    if retention_days <= 0:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    try:
        deleted = await asyncio.to_thread(storage.purge_older_than, cutoff)
        if deleted:
            log.info("audit retention: purged %d rows older than %s", deleted, cutoff)
    except Exception:
        log.exception("audit retention job failed")


# -------- main --------


SHUTDOWN_TIMEOUT_S = 15


async def amain() -> None:
    s = get_settings()
    cats.load()

    graph = GraphClient()
    storage = SqliteStorage(s.audit_db)
    rules = rules_mod.parse_rules(s.rules_file)

    addresses = load_mailboxes(s.mailboxes_file)
    mailboxes = graph.resolve_mailboxes(addresses)
    mbx_index = {m.user_id: m for m in mailboxes}
    log.info("monitoring %d unique mailboxes", len(mailboxes))
    log.debug("mailbox UPNs: %s", [m.primary_upn for m in mailboxes])

    queue = FairQueue()
    dispatcher = Dispatcher(graph, dry_run=s.dry_run)
    if s.dry_run:
        log.warning("=" * 60)
        log.warning("DRY-RUN MODE ENABLED — no messages will be moved.")
        log.warning("Classifications are recorded in the audit DB with")
        log.warning("action='dry-run' so you can review what WOULD have")
        log.warning("happened. Set DRY_RUN=false (or unset it) to enable")
        log.warning("real action.")
        log.warning("=" * 60)

    failed_folder_fetches = 0
    for mbx in mailboxes:
        try:
            dispatcher.folder_map(mbx.user_id, refresh=True)
        except GraphError as e:
            failed_folder_fetches += 1
            log.error("could not list folders for %s: %s", mbx.primary_upn, e)
    if failed_folder_fetches and failed_folder_fetches == len(mailboxes):
        log.critical("all %d mailboxes failed folder fetch — exiting",
                     failed_folder_fetches)
        graph.close()
        return

    allowed_user_ids = set(mbx_index.keys())
    app = make_app(
        queue,
        allowed_user_ids=allowed_user_ids,
        health_provider=lambda: _health_snapshot(queue, s.dry_run),
    )
    config = uvicorn.Config(app, host="0.0.0.0", port=s.port, log_level="info")
    server = uvicorn.Server(config)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(renewal_job, CronTrigger(hour="*/12"), id="subscription_renewal")
    if s.audit_retention_days > 0:
        scheduler.add_job(
            retention_job,
            CronTrigger(hour=3, minute=0),
            args=[storage, s.audit_retention_days],
            id="audit_retention",
        )
        log.info("audit retention enabled: %d days", s.audit_retention_days)
    else:
        log.info("audit retention disabled (AUDIT_RETENTION_DAYS=0)")
    scheduler.start()

    # Track the startup-renewal task so we can await it on shutdown and so
    # any exception is surfaced rather than silently lost.
    startup_renewal = asyncio.create_task(renewal_job(), name="startup-renewal")

    shutdown = asyncio.Event()

    workers = [
        asyncio.create_task(
            worker(f"w{i}", queue, graph, dispatcher, storage, rules, mbx_index, shutdown),
            name=f"worker-{i}",
        )
        for i in range(s.concurrency)
    ]
    log.info("started %d worker(s)", s.concurrency)
    log.info("startup config: provider=%s model=%s concurrency=%d retention=%dd "
             "mailboxes=%d rules=%d port=%d dry_run=%s",
             s.llm_provider, s.llm_model, s.concurrency, s.audit_retention_days,
             len(mailboxes), len(rules), s.port, s.dry_run)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    server_task = asyncio.create_task(server.serve(), name="uvicorn")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop")

    try:
        await asyncio.wait({server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        # If uvicorn died first (e.g. port in use), surface the error
        # before tearing the rest down.
        if server_task.done() and not stop_task.done():
            exc = server_task.exception()
            if exc:
                log.critical("uvicorn server task exited with exception: %r", exc)
    finally:
        log.info("shutting down")
        shutdown.set()
        server.should_exit = True

        # Wait for uvicorn to drain in-flight requests, with a deadline.
        try:
            await asyncio.wait_for(asyncio.shield(server_task), timeout=SHUTDOWN_TIMEOUT_S)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            log.warning("uvicorn did not drain within %ds; cancelling", SHUTDOWN_TIMEOUT_S)
            server_task.cancel()
            with suppress(asyncio.CancelledError, BaseException):
                await server_task
        except Exception as e:
            log.warning("uvicorn server task raised on shutdown: %r", e)

        # Stop the scheduler before workers — keeps a fresh renewal job
        # from firing while workers drain.
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            log.exception("scheduler shutdown failed")

        # Wait for the startup-renewal task with a short bound.
        if not startup_renewal.done():
            try:
                await asyncio.wait_for(startup_renewal, timeout=5)
            except (asyncio.TimeoutError, Exception):
                startup_renewal.cancel()
                with suppress(asyncio.CancelledError, BaseException):
                    await startup_renewal

        # Bounded grace period for workers to finish their in-flight job.
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*workers, return_exceptions=True),
                timeout=SHUTDOWN_TIMEOUT_S,
            )
            for w, r in zip(workers, results):
                if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                    log.error("worker %s exited with %r", w.get_name(), r)
        except asyncio.TimeoutError:
            log.warning("workers did not exit within %ds; cancelling", SHUTDOWN_TIMEOUT_S)
            for w in workers:
                w.cancel()
            with suppress(asyncio.CancelledError, BaseException):
                await asyncio.gather(*workers, return_exceptions=True)

        if not stop_task.done():
            stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await stop_task

        graph.close()
        log.info("shutdown complete")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(amain())


if __name__ == "__main__":
    main()
