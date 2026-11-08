"""Holiday-only audits without replacing ordinary collection plans/checkpoints."""
import logging

from app.services.backfill_engine import BackfillRunner
from app.services.backfill_memory import memory_resume_ready
from app.services.capital_history_connection import is_history_connection_error
from app.services.day_candle_integrity import integrity_schedule, taipei_today
from app.services.day_candle_integrity_runner import DayIntegrityAudit
from app.services.mysql_instrument_repository import MySQLRepositoryError
from app.services.taiwan_trading_calendar import latest_trading_day_on_or_before
from app.services.trading_suspensions import load_suspensions

logger = logging.getLogger(__name__)


class DayMaintenanceControl:
    """Separate progress namespace, same user pause and exclusive OS lease.

    A holiday audit must never overwrite a pending 35-group scope, retry timer,
    completed counters or run ID. Only its own progress is written here. The
    parent control's pause flag is always authoritative, including new pauses.
    """
    def __init__(self, control):
        self.control = control

    def snapshot(self):
        state = self.control.snapshot()
        return dict(state, **{**state.get('day_integrity_maintenance', {}),
                             'pause_requested': state.get('pause_requested', False)})

    def update(self, **values):
        previous = self.control.snapshot().get('day_integrity_maintenance', {})
        self.control.update(day_integrity_maintenance=dict(previous, **values))
        return self.snapshot()


def run_day_maintenance(capital, config, control, repository_factory, market_sources):
    """Caller already holds BOTH the API queue and the global worker lease."""
    allowed, reason = integrity_schedule()
    if not allowed or 'day' not in config.configured_intervals:
        return 'skipped', reason if not allowed else '設定未選日 K，不新增日 K 驗收'
    state = control.snapshot()
    progress = DayMaintenanceControl(control)
    if state.get('pause_requested'):
        progress.update(active=False, day_integrity_active=False, state='paused',
                        pause_reason='使用者已暫停；假日驗收不自行解除暫停')
        return 'paused', '使用者已暫停；保留所有計畫、斷點與缺口清單'
    if state.get('preparation_pending') or state.get('preparation_complete') is False:
        return 'paused', '完整重抓尚未準備完成；假日驗收不介入'
    if not capital.quote_ready:
        progress.update(active=False, day_integrity_active=False, state='waiting_connection',
                        pause_reason='等待使用者登入；不自動登入')
        return 'waiting_connection', '休市日日 K 驗收等待使用者登入；不自動登入'
    if not memory_resume_ready(control) or not memory_resume_ready(progress):
        progress.update(active=False, day_integrity_active=False, state='waiting_memory',
                        pause_reason='等待記憶體恢復門檻與穩定時間')
        return 'waiting_memory', '休市日日 K 驗收等待記憶體恢復門檻與穩定時間'
    # Actual closure date decides eligibility, not a saved plan's as_of_date.
    # A weekend checks through the latest trading day, never invents weekend K.
    end = latest_trading_day_on_or_before(taipei_today())
    plans = []
    for market in config.markets:
        _label, instruments, candles, jobs = market_sources[market]
        plans.append(dict(market_no=market, interval='day', batch_v2=True,
                          end_date=end, target_bars=config.target_bars_by_interval['day'],
                          instrument_table=instruments, candle_table=candles, job_table=jobs))
    progress.update(active=True, state='running', pause_kind=None, pause_reason=None,
                    day_integrity_active=True, day_integrity_schedule_reason=reason,
                    as_of_date=end, windows=0, processed=0, completed=0,
                    insufficient_history=0, failed=0, day_gap_date=None,
                    day_integrity_checked=0, day_integrity=None, next_retry_at=None)
    try:
        runner = BackfillRunner(capital, repository_factory(), progress)
        runner.suspensions = load_suspensions()
        audit = DayIntegrityAudit(runner, allowed=lambda: integrity_schedule()[0]).run(plans)
        if audit.get('schedule_deferred'):
            progress.update(state='scheduled', pause_reason='已進入交易日；保留缺口至下次休市日')
            return 'skipped', '已進入交易日；不再啟動驗收或缺口請求，已提交資料保留'
        if audit.get('stopped') is not None:
            stopped = audit['stopped']
            return ('waiting_memory' if stopped.get('pause_kind') == 'memory' else 'paused'), stopped['pause_reason']
        summary = audit['summary']
        status = 'waiting_backfill' if audit['blocked'] else 'waiting_review' if audit.get('pending_review') else 'completed'
        progress.update(state=status,
                        next_retry_at=audit['retry_at'].isoformat()+'Z' if audit['blocked'] else None)
        message = (f'休市日日 K 驗收：已檢查 {summary["checked_symbols"]} 檔，'
                   f'待查 {summary.get("unresolved_symbols",summary["blocked_symbols"])} 檔／{summary["unresolved_dates"]} 個日期；'
                   f'本輪查詢 {summary["query_windows"]} 個區間；一般抓取計畫與斷點未重排')
        if audit.get('pending_review'):
            reviews = audit.get('pending_review_summary') or {}
            if reviews.get('symbols') is not None:
                message += f'；空回應待查 {reviews["symbols"]} 檔／{reviews["dates"]} 個日期'
                if reviews.get('periods'):
                    message += f'（另 {reviews["periods"]} 個週月期間）'
            else:
                message += f'；{audit["pending_review"]} 筆空回應待查紀錄'
        return status, message
    except Exception as error:
        status = ('waiting_database' if isinstance(error, MySQLRepositoryError) else
                  'waiting_connection' if is_history_connection_error(error) else 'error')
        progress.update(state=status, pause_reason=str(error))
        logger.exception('休市日日 K 驗收未完成；一般抓取計畫與斷點保留')
        return status, f'休市日日 K 驗收尚未完成：{error}'
    finally:
        progress.update(active=False, day_integrity_active=False)
