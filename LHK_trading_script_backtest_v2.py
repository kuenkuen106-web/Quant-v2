# =============================================================================
# ⚙️ UAT 時光機模式 (The Ultimate Edition - Full Integration)
# 核心功能：模擬過去交易日 / 波段與短線雙引擎 / 大盤 FTD 偵測 / 部位計算機
# =============================================================================

import pandas as pd, numpy as np, yfinance as yf, matplotlib
matplotlib.use('Agg') # 伺服器端繪圖必須加上這行
import matplotlib.pyplot as plt, matplotlib.dates as mdates, concurrent.futures
import warnings, os, datetime, json, logging, time, requests
from io import StringIO

# 關閉不必要嘅警告，保持 Terminal 乾淨
logging.getLogger('yfinance').setLevel(logging.CRITICAL)
warnings.filterwarnings('ignore')
plt.style.use('dark_background')
plt.ioff()

# =============================================================================
# 系統環境設定 (路徑與 Webhook - UAT 專用)
# =============================================================================
# 寫入 UAT 子資料夾，避免覆蓋正式版
OUTPUT_DIR = "docs/UAT"
CHARTS_DIR = os.path.join(OUTPUT_DIR, "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

# 讀取 GitHub Secrets (UAT 專用的 Webhook)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_BACKTEST_WEBHOOK_URL", "")
DISCORD_SUMMARY_WEBHOOK = os.environ.get("DISCORD_BACKTEST_SUMMARY_WEBHOOK", "")
HISTORY_FILE = os.path.join(OUTPUT_DIR, "uat_trade_history.json")

# =============================================================================
# 核心策略與時光機參數 
# =============================================================================
LOOKBACK_YEARS = 3
PQR_SWING_MIN = 75
FTD_VALID_DAYS = 20
MAX_ACCOUNT_RISK_PCT = 0.01 # 每單最多虧損總資金的 1%

# 👇 時光機設定：從 GitHub Actions 讀取要回溯幾多日 (預設回溯 10 日)
raw_days = os.environ.get("UAT_DAYS_AGO", "10")
SIMULATE_DAYS_AGO = int(raw_days)

# =============================================================================
# 功能函數區
# =============================================================================
def send_discord_alert(ticker, strategy_name, price, sl, tp, is_bullish, sources):
    if not DISCORD_WEBHOOK_URL: return
    unit = "¥" if ticker.endswith(".T") else "$"
    source_str = " | ".join(sources) if sources else "動態掃描"
    color = 65280 if is_bullish else 16711680 
    
    embed_data = {
        "title": f"🚨 [UAT 模擬] 系統異動觸發: {ticker}",
        "description": f"**{strategy_name}** 條件已達成！\n🔍 來源: `{source_str}`",
        "color": color,
        "fields": [
            {"name": "💵 模擬當時價格", "value": f"{unit}{price}", "inline": True},
            {"name": "🛑 建議止損", "value": f"{unit}{sl}", "inline": True},
            {"name": "🎯 建議止盈", "value": f"{unit}{tp}", "inline": True}
        ],
        "footer": {"text": f"時光機模式執行中 | 模擬日期: {today_str}"}
    }
    try: 
        res = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed_data]})
        if res.status_code == 429: print(f"⚠️ Discord 拒絕接收 - 傳送太快！")
        time.sleep(0.5) 
    except Exception as e: print(f"⚠️ Discord 連線錯誤: {e}")

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return []
    return []

trade_history = load_history()

# =============================================================================
# MODULE 1 & 2 — 雙市場數據引擎與時光機截斷
# =============================================================================
print(f"⏳ [1-3/7] 正在抓取數據與啟動時光機 (回溯 {SIMULATE_DAYS_AGO} 日)...")

def build_dynamic_watchlist():
    ticker_sources = {}
    def add_to_map(tickers, source_label):
        for t in tickers:
            if not isinstance(t, str) or len(t) < 1: continue
            clean_t = t.strip()
            if not clean_t.endswith('.T'): clean_t = clean_t.replace('.', '-')
            if clean_t not in ticker_sources: ticker_sources[clean_t] = []
            if source_label not in ticker_sources[clean_t]: ticker_sources[clean_t].append(source_label)
    
    try:
        csv_url = "https://raw.githubusercontent.com/datasets/s-p-500-companies/master/data/constituents.csv"
        df_sp = pd.read_csv(csv_url, timeout=10)
        add_to_map(df_sp['Symbol'].tolist(), "S&P500")
    except:
        add_to_map(["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"], "S&P500")

    add_to_map(['SPY', '^VIX', '^N225'], "基準指數")
    return ticker_sources

TICKER_MAP = build_dynamic_watchlist()
ALL_TICKERS = list(TICKER_MAP.keys())

data_raw = yf.download(ALL_TICKERS, period=f"{LOOKBACK_YEARS}y", progress=False, threads=True, timeout=30, group_by='column')
if isinstance(data_raw.columns, pd.MultiIndex):
    closes, highs, lows, vols, opens = data_raw['Close'].ffill(), data_raw['High'].ffill(), data_raw['Low'].ffill(), data_raw['Volume'].ffill(), data_raw['Open'].ffill()
else:
    closes = data_raw[['Close']].ffill(); highs = data_raw[['High']].ffill(); lows = data_raw[['Low']].ffill(); vols = data_raw[['Volume']].ffill(); opens = data_raw[['Open']].ffill()

# ---------------------------------------------------------------------
# 🕒 【時光機關鍵邏輯】抹除「未來」數據
# ---------------------------------------------------------------------
if SIMULATE_DAYS_AGO > 0:
    print(f"⏰ [時光機] 正在抹除最近 {SIMULATE_DAYS_AGO} 天數據，回溯中...")
    closes = closes.iloc[:-SIMULATE_DAYS_AGO]
    highs = highs.iloc[:-SIMULATE_DAYS_AGO]
    lows = lows.iloc[:-SIMULATE_DAYS_AGO]
    vols = vols.iloc[:-SIMULATE_DAYS_AGO]
    opens = opens.iloc[:-SIMULATE_DAYS_AGO]
# ---------------------------------------------------------------------

# 👇 絕對唔可以用 datetime.now()！必須用截斷後 DataFrame 嘅最後一日！
today_str = closes.index[-1].strftime('%Y-%m-%d')
print(f"📅 [UAT] 模擬今日日期：{today_str}")

# =============================================================================
# MODULE 3 — 雙市場宏觀剖析 (FTD, 市寬, 派發日 獨立計算)
# =============================================================================
vix_c = closes['^VIX'].ffill()

jp_tickers = [t for t in closes.columns if str(t).endswith('.T')]
us_tickers = [t for t in closes.columns if not str(t).endswith('.T') and t not in ['SPY', '^VIX', '^N225']]

def calc_breadth(ticker_list):
    if not ticker_list: return 0
    sub_closes = closes[ticker_list]
    breadth = (sub_closes > sub_closes.rolling(50).mean()).sum(axis=1) / sub_closes.shape[1] * 100
    return round(float(breadth.iloc[-1]), 1)

us_breadth = calc_breadth(us_tickers)
jp_breadth = calc_breadth(jp_tickers)

def calc_macro_regime(index_ticker):
    idx_c, idx_v, idx_l = closes[index_ticker], vols[index_ticker], lows[index_ticker]
    ret = idx_c.pct_change()
    dist_mask = (ret < -0.002) & (idx_v > idx_v.shift(1))
    curr_dist_days = int(dist_mask.rolling(25).sum().iloc[-1])
    
    ftd_history = np.zeros(len(idx_c))
    rally_day, rally_low, last_ftd_idx = 0, float('inf'), -999
    
    for i in range(1, len(idx_c)):
        c, pc, l, v, pv = idx_c.iloc[i], idx_c.iloc[i-1], idx_l.iloc[i], idx_v.iloc[i], idx_v.iloc[i-1]
        if l < rally_low: rally_low, rally_day = l, 1 if c > pc else 0
        else:
            if c > pc: rally_day = max(1, rally_day + 1)
            elif rally_day > 0: rally_day += 1
        if rally_day >= 4 and c > pc * 1.012 and v > pv:
            last_ftd_idx, rally_low, rally_day = i, c, 0
        ftd_history[i] = (i - last_ftd_idx) if last_ftd_idx > 0 else 999
        
    curr_ftd_days = int(ftd_history[-1])
    is_bull = float(idx_c.iloc[-1]) > float(idx_c.rolling(200).mean().iloc[-1])
    
    if vix_c.iloc[-1] > 25: status, color = "🚨 VIX 恐慌警戒", "text-red-500 bg-red-500/20 border-red-500/50"
    elif is_bull: status, color = "🟢 牛市格局", "text-emerald-500 bg-emerald-500/10 border-emerald-500/20"
    elif curr_ftd_days <= FTD_VALID_DAYS: status, color = f"✅ 底部確認 ({curr_ftd_days}日 FTD)", "text-blue-400 bg-blue-500/10 border-blue-500/20"
    else: status, color = "❌ 熊市空頭", "text-red-500 bg-red-500/10 border-red-500/20"
    
    return curr_dist_days, is_bull, status, color

us_dist, us_is_bull, us_status, us_color = calc_macro_regime('SPY')
jp_dist, jp_is_bull, jp_status, jp_color = calc_macro_regime('^N225')

# 為 UAT 繪製 SPY 圖表
spy_c, spy_v, spy_l = closes['SPY'], vols['SPY'], lows['SPY']
spy_20, spy_50, spy_200 = spy_c.rolling(20).mean(), spy_c.rolling(50).mean(), spy_c.rolling(200).mean()
fig, ax = plt.subplots(figsize=(8, 3), dpi=100)
ax.plot(spy_c.index[-200:], spy_c.iloc[-200:], color='#cbd5e1', label='SPX', linewidth=1.5)
ax.plot(spy_20.index[-200:], spy_20.iloc[-200:], color='#3b82f6', label='20MA', linewidth=1, alpha=0.8)
ax.plot(spy_50.index[-200:], spy_50.iloc[-200:], color='#f59e0b', label='50MA', linewidth=1, alpha=0.8)
ax.plot(spy_200.index[-200:], spy_200.iloc[-200:], color='#dc2626', label='200MA', linestyle='-.', linewidth=1.5)
fig.patch.set_facecolor('#0f172a'); ax.set_facecolor('#0f172a')
ax.tick_params(colors='white', labelsize=8)
ax.legend(facecolor='#1e293b', labelcolor='white', loc='upper left', ncol=3, fontsize=8)
for spine in ax.spines.values(): spine.set_edgecolor('#334155')
plt.tight_layout()
plt.savefig(os.path.join(CHARTS_DIR, "SPY_Trend.png"), transparent=True)
plt.close(fig)

r126 = closes / closes.shift(126) - 1
r252 = closes / closes.shift(252) - 1
rs_rank = ((0.6 * r126) + (0.4 * r252)).rank(axis=1, pct=True) * 99 + 1
rs_momentum = rs_rank - rs_rank.shift(20)

# =============================================================================
# MODULE 4 & 5 — 雙策略判定引擎與自動結算
# =============================================================================
print(f"⏳ [4-6/7] 正在按 {today_str} 視角進行策略演算...")

current_prices = closes.iloc[-1].to_dict()
closed_this_run = []
for trade in trade_history:
    if trade.get('status') == 'OPEN':
        tk = trade.get('tk')
        if tk in current_prices and not pd.isna(current_prices[tk]):
            now_px = round(float(current_prices[tk]), 2)
            trade['last_px'] = now_px
            tp, sl = trade.get('tp'), trade.get('sl')
            if tp and now_px >= tp:
                trade['status'], trade['close_date'] = '✅ TAKE PROFIT', today_str
                closed_this_run.append(trade)
            elif sl and now_px <= sl:
                trade['status'], trade['close_date'] = '❌ STOP LOSS', today_str
                closed_this_run.append(trade)

swing_results, short_term_results, js_payload = [], [], []

for ticker in [t for t in ALL_TICKERS if t not in ['SPY','^VIX','^N225']]:
    try:
        c_raw = closes[ticker].dropna()
        if len(c_raw) < 252 + 200: continue
        c, h, l, v, op = closes[ticker], highs[ticker], lows[ticker], vols[ticker], opens[ticker]
        cp = float(c.iloc[-1])
        if (c.tail(20) * v.tail(20)).mean() < (300_000_000 if ticker.endswith('.T') else 5_000_000): continue

        is_jp = ticker.endswith('.T')
        ticker_is_bull = jp_is_bull if is_jp else us_is_bull

        rs = rs_rank[ticker].iloc[-1]
        rs_mom = rs_momentum[ticker].iloc[-1]
        if pd.isna(rs) or rs < PQR_SWING_MIN: continue

        sma20, std20 = c.rolling(20).mean(), c.rolling(20).std()
        bb_lower, bb_width = sma20 - (2 * std20), (4 * std20) / sma20
        atr = (h-l).rolling(14).mean(); catr = float(atr.iloc[-1])
        
        delta = c.diff()
        rsi = 100 - (100 / (1 + (delta.where(delta > 0, 0)).rolling(14).mean() / (-delta.where(delta < 0, 0)).rolling(14).mean()))
        
        base_dd = (c.rolling(60).max() - c.rolling(60).min()) / c.rolling(60).max()
        rec_volat = (c.rolling(10).max() - c.rolling(10).min()) / c.rolling(10).max()
        is_vcp = (base_dd.iloc[-1] <= 0.35) and (rec_volat.iloc[-1] <= 0.06) and (v.iloc[-1] < v.rolling(50).mean().iloc[-1])
        is_bb_sqz = (bb_width.iloc[-1] <= bb_width.rolling(120).min().iloc[-1] * 1.1)

        trade_info = None 
        tag_name = ""
        sl_p, tp_p = 0, 0
        risk_per_share = 0

        if (is_vcp or is_bb_sqz) and ticker_is_bull:
            tag_name = "🏆 VCP 突破" if is_vcp else "💥 BB 擠壓"
            sl_p, tp_p = round(cp - 2.5 * catr, 2), round(cp + 4.5 * catr, 2)
            risk_per_share = cp - sl_p
            swing_results.append({'tk': ticker, 'rs': round(rs,0), 'mom': round(rs_mom,1), 'px': round(cp,2), 'sl': sl_p, 'tp': tp_p, 'tag': tag_name})
            trade_info = {'date': today_str, 'tk': ticker, 'px': round(cp, 2), 'sl': sl_p, 'tp': tp_p, 'last_px': round(cp, 2), 'status': 'OPEN', 'tag': tag_name}
        
        elif not trade_info: 
            is_gap_up = ((op.iloc[-1] - c.iloc[-2]) / c.iloc[-2] >= 0.03) and (v.iloc[-1] > v.rolling(20).mean().iloc[-1] * 2)
            is_oversold = (rsi.iloc[-1] < 28) and (cp < bb_lower.iloc[-1])
            if is_gap_up or is_oversold:
                tag_name = "⚡ 缺口動能" if is_gap_up else "📉 極度超賣"
                sl_p, tp_p = round(cp * 0.95, 2), round(cp * 1.05, 2)
                risk_per_share = cp - sl_p
                short_term_results.append({'tk': ticker, 'rs': round(rs,0), 'mom': round(rs_mom,1), 'px': round(cp,2), 'sl': sl_p, 'tp': tp_p, 'tag': tag_name})
                trade_info = {'date': today_str, 'tk': ticker, 'px': round(cp, 2), 'sl': sl_p, 'tp': tp_p, 'last_px': round(cp, 2), 'status': 'OPEN', 'tag': tag_name}

        if trade_info:
            send_discord_alert(ticker, tag_name, round(cp, 2), sl_p, tp_p, True, [])
            if not any(t.get('tk') == ticker and t.get('status') == 'OPEN' for t in trade_history):
                 trade_history.append(trade_info)
            
            js_payload.append({
                "ticker": ticker, "tag": tag_name, "curr_price": round(cp, 2), 
                "sl_price": sl_p, "tp_price": tp_p, "risk_per_share": risk_per_share
            })

    except Exception as e: pass

swing_results.sort(key=lambda x: x['rs'], reverse=True)
short_term_results.sort(key=lambda x: x['rs'], reverse=True)

with open(HISTORY_FILE, "w", encoding="utf-8") as f: json.dump(trade_history[-150:], f, indent=4)

# =============================================================================
# MODULE 6 — 總結算與 Discord Report
# =============================================================================
print("⏳ [6/7] 正在結算戰績並發送 Discord 報告...")

def calculate_stats(history):
    closed = [t for t in history if '✅' in t['status'] or '❌' in t['status']]
    if not closed: return 0, 0, 0
    wins = [t for t in closed if '✅' in t['status']]
    return len(closed), len(wins), round(len(wins)/len(closed)*100, 1)

total_closed, wins, win_rate = calculate_stats(trade_history)

if DISCORD_SUMMARY_WEBHOOK:
    detail_lines = []
    if closed_this_run:
        for t in closed_this_run:
            icon = "🎯" if "TAKE PROFIT" in t['status'] else "🛑"
            shares = 10000 / t['px']
            pnl = shares * (t['last_px'] - t['px'])
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            detail_lines.append(f"{icon} **{t['tk']}** ({t.get('tag', 'N/A')}): {pnl_str}")
    details_text = "\n".join(detail_lines) if detail_lines else "今日無新結案交易。"

    open_trades = [t for t in trade_history if t.get('status') == 'OPEN']
    floating_pnl = sum([(10000 / t['px']) * (t['last_px'] - t['px']) for t in open_trades])
    floating_str = f"+${floating_pnl:.2f}" if floating_pnl >= 0 else f"-${abs(floating_pnl):.2f}"
    floating_color = 65280 if floating_pnl >= 0 else 16711680

    strategy_stats = {}
    for t in [x for x in trade_history if '✅' in x['status'] or '❌' in x['status']]:
        tag = t.get('tag', '未分類')
        if tag not in strategy_stats: strategy_stats[tag] = {'wins': 0, 'total': 0, 'pnl': 0}
        strategy_stats[tag]['total'] += 1
        if '✅' in t['status']: strategy_stats[tag]['wins'] += 1
        strategy_stats[tag]['pnl'] += (10000 / t['px']) * (t['last_px'] - t['px'])
    
    breakdown_lines = []
    for tag, st in strategy_stats.items():
        w_rate = round((st['wins'] / st['total']) * 100, 1) if st['total'] > 0 else 0
        pnl_s = f"+${st['pnl']:.0f}" if st['pnl'] >= 0 else f"-${abs(st['pnl']):.0f}"
        breakdown_lines.append(f"**{tag}**: {w_rate}% 勝率 | P&L: {pnl_s} ({st['total']}單)")
    breakdown_text = "\n".join(breakdown_lines) if breakdown_lines else "尚無足夠結案數據。"

    us_macro_str = f"狀態: **{us_status}**\n市寬: {us_breadth}%\n派發: {us_dist} 日"
    jp_macro_str = f"狀態: **{jp_status}**\n市寬: {jp_breadth}%\n派發: {jp_dist} 日"

    payload = {
        "embeds": [{
            "title": f"📊 [UAT 模擬] 系統戰績與宏觀結算 ({today_str})", 
            "description": f"**今日結案動態:**\n{details_text}\n\n**🔍 各策略歷史表現:**\n{breakdown_text}",
            "color": floating_color,
            "fields": [
                {"name": "🇺🇸 美股大盤 (SPX)", "value": us_macro_str, "inline": True},
                {"name": "🇯🇵 日股大盤 (N225)", "value": jp_macro_str, "inline": True},
                {"name": '\u200b', "value": '\u200b', "inline": False},
                {"name": "📂 目前持倉", "value": f"{len(open_trades)} 隻", "inline": True},
                {"name": "🌊 總浮動盈虧", "value": f"**{floating_str}**", "inline": True},
                {"name": "📈 總勝率", "value": f"{win_rate}% ({wins}/{total_closed})", "inline": True}
            ],
            "footer": {"text": f"每單本金 $10,000 USD | 時光機回溯 {SIMULATE_DAYS_AGO} 日"}
        }]
    }
    try: requests.post(DISCORD_SUMMARY_WEBHOOK, json=payload)
    except: pass

# =============================================================================
# MODULE 7 — 生成 UAT 前端 HTML
# =============================================================================
print("⏳ [7/7] 正在生成完整型雙策略儀表板 (UAT版)...")

def get_unit(tk): return "¥" if tk.endswith(".T") else "$"

html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <script src="https://cdn.tailwindcss.com"></script>
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
    <title>UAT QUANT ({today_str})</title>
</head>
<body class="bg-[#020617] text-slate-300 p-4 font-sans h-screen flex flex-col overflow-hidden">
    
    <header class="bg-slate-900 border border-slate-800 rounded-xl p-3 shrink-0 mb-4 shadow-lg flex flex-col gap-3 relative overflow-hidden">
        <div class="absolute -right-10 -top-10 opacity-5 pointer-events-none transform rotate-12">
            <span class="text-9xl font-black italic">UAT TEST</span>
        </div>
        
        <div class="flex justify-between items-center z-10">
            <div>
                <h1 class="text-2xl font-black text-white italic tracking-tighter">UAT場 <span class="text-fuchsia-500">QUANT</span></h1>
                <div class="mt-2 inline-block px-3 py-1 bg-fuchsia-500/20 border border-fuchsia-500/30 rounded-full text-fuchsia-400 text-[10px] font-black tracking-widest shadow-[0_0_15px_rgba(217,70,239,0.2)]">
                    🕰️ 模擬時光機日期: {today_str}
                </div>
            </div>
            <div class="text-xs font-black text-slate-500 bg-black/50 px-3 py-1 rounded-lg border border-slate-800">
                🌐 Dual-Market Macro Radar
            </div>
        </div>

        <div class="grid grid-cols-2 gap-4 z-10">
            <div class="flex items-center gap-2 bg-slate-800/30 p-2 rounded-lg border border-slate-800">
                <div class="w-12 text-center text-xs font-black text-slate-400 border-r border-slate-700">美股<br>SPX</div>
                <div class="flex-1 flex justify-between gap-2 px-2">
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">市寬</span><span class="text-xs font-bold {'text-emerald-400' if us_breadth>40 else 'text-red-400'}">{us_breadth}%</span></div>
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">派發</span><span class="text-xs font-bold {'text-red-400' if us_dist>=5 else 'text-emerald-400'}">{us_dist}d</span></div>
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">狀態</span><span class="text-[10px] font-bold px-1 rounded {us_color}">{us_status}</span></div>
                </div>
            </div>
            <div class="flex items-center gap-2 bg-slate-800/30 p-2 rounded-lg border border-slate-800">
                <div class="w-12 text-center text-xs font-black text-slate-400 border-r border-slate-700">日股<br>N225</div>
                <div class="flex-1 flex justify-between gap-2 px-2">
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">市寬</span><span class="text-xs font-bold {'text-emerald-400' if jp_breadth>40 else 'text-red-400'}">{jp_breadth}%</span></div>
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">派發</span><span class="text-xs font-bold {'text-red-400' if jp_dist>=5 else 'text-emerald-400'}">{jp_dist}d</span></div>
                    <div class="flex flex-col"><span class="text-[8px] text-slate-500">狀態</span><span class="text-[10px] font-bold px-1 rounded {jp_color}">{jp_status}</span></div>
                </div>
            </div>
        </div>
    </header>

    <main class="flex-1 flex gap-4 overflow-hidden z-10">
        <div class="w-1/3 flex flex-col gap-4 overflow-hidden">
            <div class="bg-slate-900 p-2 rounded-xl border border-slate-800 h-[200px] shrink-0 relative flex items-center justify-center shadow-lg">
                <div class="absolute top-2 left-3 z-10 flex gap-2 items-center">
                    <span class="text-xs font-bold text-slate-400">SPX Anatomy:</span>
                    <span class="text-[9px] bg-red-500/20 text-red-400 px-1 rounded border border-red-500/30">200MA</span>
                    <span class="text-[9px] text-emerald-400 ml-2">▲ FTD</span>
                </div>
                <img src="charts/SPY_Trend.png" class="max-h-full max-w-full object-contain">
            </div>

            <div class="bg-slate-900 rounded-xl border border-slate-800 flex-1 flex flex-col overflow-hidden shadow-lg">
                <div class="p-3 border-b border-slate-800 font-black text-fuchsia-400 flex justify-between items-center shrink-0">
                    <span>🎯 模擬推介信號 (點擊查看)</span>
                </div>
                <div class="overflow-y-auto flex-1 p-2 space-y-2" id="signal-list">
                    <div class="text-[10px] font-bold text-slate-500 uppercase ml-1 mt-2">🏆 波段策略 (Swing)</div>
                    {"".join([f'''
                    <div class="bg-slate-800/50 hover:bg-fuchsia-900/30 cursor-pointer border border-slate-700/50 hover:border-fuchsia-500/50 rounded-lg p-2 transition" onclick="loadContent('{d['tk']}')">
                        <div class="flex justify-between items-center">
                            <span class="font-black text-white text-sm">{d['tk']}</span>
                            <span class="text-[9px] bg-fuchsia-500/20 text-fuchsia-300 px-1.5 py-0.5 rounded">{d['tag']}</span>
                        </div>
                        <div class="flex justify-between text-[10px] text-slate-400 mt-1">
                            <span>RS: {d['rs']} (<span class="{ 'text-emerald-400' if d['mom']>0 else 'text-red-400'}">{'+' if d['mom']>0 else ''}{d['mom']}</span>)</span>
                            <span>現價: {get_unit(d['tk'])}{d['px']}</span>
                        </div>
                    </div>
                    ''' for d in swing_results]) if swing_results else '<p class="text-slate-600 italic text-xs px-2">無訊號</p>'}
                    
                    <div class="text-[10px] font-bold text-slate-500 uppercase ml-1 mt-4">⚡ 短線游擊 (Short Term)</div>
                    {"".join([f'''
                    <div class="bg-slate-800/50 hover:bg-amber-900/30 cursor-pointer border border-slate-700/50 hover:border-amber-500/50 rounded-lg p-2 transition" onclick="loadContent('{d['tk']}')">
                        <div class="flex justify-between items-center">
                            <span class="font-black text-white text-sm">{d['tk']}</span>
                            <span class="text-[9px] bg-amber-500/20 text-amber-300 px-1.5 py-0.5 rounded">{d['tag']}</span>
                        </div>
                        <div class="flex justify-between text-[10px] text-slate-400 mt-1">
                            <span>RS: {d['rs']}</span>
                            <span>現價: {get_unit(d['tk'])}{d['px']}</span>
                        </div>
                    </div>
                    ''' for d in short_term_results]) if short_term_results else '<p class="text-slate-600 italic text-xs px-2">無訊號</p>'}
                </div>
            </div>
        </div>

        <div class="w-2/3 flex flex-col gap-4 h-full">
            <div class="bg-slate-900 rounded-xl border border-slate-700 p-4 shrink-0 shadow-lg">
                <div class="flex justify-between items-center mb-3">
                    <div class="flex items-center gap-2">
                        <h3 class="text-sm font-black text-amber-500">🧮 專業部位計算機 (Trade Execution Plan)</h3>
                        <span id="calc_ticker_name" class="text-xs font-bold text-white bg-slate-700 px-2 py-0.5 rounded">-</span>
                    </div>
                    <div class="flex items-center gap-2">
                        <label class="text-[10px] text-slate-400 font-bold uppercase">總資金 (Account Size):</label>
                        <input type="number" id="acc_size" value="10000" class="bg-slate-800 border border-slate-600 text-white text-xs px-2 py-1 rounded w-24 text-right focus:outline-none focus:border-amber-500" onchange="updateCalculator()" onkeyup="updateCalculator()">
                    </div>
                </div>
                <div class="grid grid-cols-5 gap-3 text-center">
                    <div class="bg-slate-800/50 p-2 rounded-lg border border-slate-700">
                        <div class="text-[9px] text-slate-400 uppercase font-bold">進場現價</div>
                        <div class="font-black text-white text-lg" id="calc_entry">-</div>
                    </div>
                    <div class="bg-red-900/10 p-2 rounded-lg border border-red-900/50">
                        <div class="text-[9px] text-red-400 uppercase font-bold">嚴格止損 (-2.5 ATR)</div>
                        <div class="font-black text-red-400 text-lg" id="calc_sl">-</div>
                    </div>
                    <div class="bg-emerald-900/10 p-2 rounded-lg border border-emerald-900/50">
                        <div class="text-[9px] text-emerald-400 uppercase font-bold">目標止盈 (+4.5 ATR)</div>
                        <div class="font-black text-emerald-400 text-lg" id="calc_tp">-</div>
                    </div>
                    <div class="bg-amber-500/10 p-2 rounded-lg border border-amber-500/30 relative">
                        <div class="absolute -top-2 -right-2 bg-amber-500 text-black text-[8px] font-black px-1.5 py-0.5 rounded-full">1% Risk</div>
                        <div class="text-[9px] text-amber-500 uppercase font-bold">建議買入股數</div>
                        <div class="font-black text-amber-400 text-lg" id="calc_shares">-</div>
                    </div>
                    <div class="bg-slate-800/50 p-2 rounded-lg border border-slate-700">
                        <div class="text-[9px] text-slate-400 uppercase font-bold">總持倉成本 (佔比)</div>
                        <div class="font-black text-blue-300 text-lg" id="calc_cost">-</div>
                    </div>
                </div>
            </div>

            <div class="bg-slate-900 p-1 rounded-xl border border-slate-800 flex-1 relative shadow-lg" id="tv_chart_container">
                <div class="absolute inset-0 flex items-center justify-center text-slate-600 text-sm italic font-bold z-0 pointer-events-none">
                    請點擊左側信號以載入圖表
                </div>
            </div>
        </div>
    </main>

    <script>
        const rawData = {json.dumps(js_payload)};
        let currentSelectedTicker = null;
        let tvWidget = null;

        function loadContent(ticker) {{
            currentSelectedTicker = ticker;
            
            const isJp = ticker.endsWith('.T');
            const tvSymbol = isJp ? 'TSE:' + ticker.replace('.T', '') : ticker;

            if (tvWidget) {{ tvWidget.remove(); }}
            tvWidget = new TradingView.widget({{
                "autosize": true, "symbol": tvSymbol, "interval": "D", "timezone": "Etc/UTC",
                "theme": "dark", "style": "1", "locale": "en", "container_id": "tv_chart_container"
            }});

            updateCalculator();
        }}

        function updateCalculator() {{
            if (!currentSelectedTicker) return;
            const data = rawData.find(d => d.ticker === currentSelectedTicker);
            if (!data) return;

            const isJp = data.ticker.endsWith('.T');
            const unit = isJp ? '¥' : '$';

            document.getElementById('calc_ticker_name').innerText = data.ticker + " (" + data.tag + ")";
            
            const accountSize = parseFloat(document.getElementById('acc_size').value) || 10000;
            const riskAmount = accountSize * {MAX_ACCOUNT_RISK_PCT};
            
            let shares = Math.floor(riskAmount / data.risk_per_share);
            if (shares <= 0) shares = 0;
            
            const totalCost = shares * data.curr_price;
            const actualPosPct = (accountSize > 0) ? (totalCost / accountSize * 100).toFixed(1) : 0;
            
            document.getElementById('calc_entry').innerText = unit + data.curr_price.toFixed(2);
            document.getElementById('calc_sl').innerText = unit + data.sl_price.toFixed(2);
            document.getElementById('calc_tp').innerText = unit + data.tp_price.toFixed(2);
            document.getElementById('calc_shares').innerText = shares;
            document.getElementById('calc_cost').innerText = unit + totalCost.toLocaleString(undefined, {{maximumFractionDigits: 0}}) + " (" + actualPosPct + "%)";
        }}
    </script>
</body>
</html>"""

with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f: f.write(html)
print(f"\n🎉 UAT 時光機版建置完成！")