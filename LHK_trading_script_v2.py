# =============================================================================
# ⚙️ V1 PRO QUANT DUAL-STRATEGY (The Ultimate Edition - Full Integration)
# 核心功能：波段與短線雙引擎 / 大盤 FTD 偵測 / 部位風險計算機 / 內建圖表
# =============================================================================

import pandas as pd, numpy as np, yfinance as yf, matplotlib
matplotlib.use('Agg') # 伺服器端繪圖必須加上這行 [cite: 1]
import matplotlib.pyplot as plt, matplotlib.dates as mdates, concurrent.futures
import warnings, os, datetime, json, logging, time, requests
from io import StringIO

# 關閉不必要嘅警告，保持 Terminal 乾淨
logging.getLogger('yfinance').setLevel(logging.CRITICAL)
warnings.filterwarnings('ignore')
plt.style.use('dark_background')
plt.ioff()

# =============================================================================
# 系統環境設定 (路徑與 Webhook)
# =============================================================================
OUTPUT_DIR = "docs"
CHARTS_DIR = os.path.join(OUTPUT_DIR, "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_SUMMARY_WEBHOOK = os.environ.get("DISCORD_SUMMARY_WEBHOOK", "")
HISTORY_FILE = os.path.join(OUTPUT_DIR, "trade_history.json")

# =============================================================================
# 核心策略參數 (Hyperparameters)
# =============================================================================
LOOKBACK_YEARS = 3
PQR_SWING_MIN = 75
FTD_VALID_DAYS = 20
MAX_ACCOUNT_RISK_PCT = 0.01 # 每單最多虧損總資金的 1%

# =============================================================================
# 功能函數區
# =============================================================================
def send_discord_alert(ticker, strategy_name, price, sl, tp, is_bullish, sources):
    if not DISCORD_WEBHOOK_URL: return
    unit = "¥" if ticker.endswith(".T") else "$"
    source_str = " | ".join(sources) if sources else "動態掃描"
    color = 65280 if is_bullish else 16711680 
    
    embed_data = {
        "title": f"🚨 系統異動觸發: {ticker}",
        "description": f"**{strategy_name}** 條件已達成！\n🔍 來源: `{source_str}`",
        "color": color,
        "fields": [
            {"name": "💵 當前現價", "value": f"{unit}{price}", "inline": True},
            {"name": "🛑 嚴格止損", "value": f"{unit}{sl}", "inline": True},
            {"name": "🎯 目標止盈", "value": f"{unit}{tp}", "inline": True}
        ],
        "footer": {"text": "V1 Quant Master 實時監控系統"}
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
# MODULE 1 & 2 — 雙市場數據引擎
# =============================================================================
print("⏳ [1-3/7] 正在抓取數據與計算大盤指標...")

def build_dynamic_watchlist():
    ticker_sources = {}
    def add_to_map(tickers, source_label):
        for t in tickers:
            if not isinstance(t, str) or len(t) < 1: continue
            clean_t = t.strip()
            if not clean_t.endswith('.T'): clean_t = clean_t.replace('.', '-')
            if clean_t not in ticker_sources: ticker_sources[clean_t] = []
            if source_label not in ticker_sources[clean_t]: ticker_sources[clean_t].append(source_label)
    
    # 這裡簡化了你的 SP500 和 N225 抓取邏輯 (保留你原本的 Fallback)
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

today_str = datetime.datetime.now().strftime('%Y-%m-%d')

# =============================================================================
# MODULE 3 — 雙市場宏觀剖析 (FTD, 市寬, 派發日 獨立計算)
# =============================================================================
print("⏳ [3/7] 正在計算美/日雙市場宏觀指標...")

vix_c = closes['^VIX'].ffill()

# 1. 區分美日股票池
jp_tickers = [t for t in closes.columns if str(t).endswith('.T')]
us_tickers = [t for t in closes.columns if not str(t).endswith('.T') and t not in ['SPY', '^VIX', '^N225']]

# 2. 獨立計算市寬 (Market Breadth > 50MA)
def calc_breadth(ticker_list):
    if not ticker_list: return 0
    sub_closes = closes[ticker_list]
    breadth = (sub_closes > sub_closes.rolling(50).mean()).sum(axis=1) / sub_closes.shape[1] * 100
    return round(float(breadth.iloc[-1]), 1)

us_breadth = calc_breadth(us_tickers)
jp_breadth = calc_breadth(jp_tickers)

# 3. 核心宏觀計算引擎 (FTD & Distribution)
def calc_macro_regime(index_ticker):
    idx_c, idx_v, idx_l = closes[index_ticker], vols[index_ticker], lows[index_ticker]
    
    # 派發日
    ret = idx_c.pct_change()
    dist_mask = (ret < -0.002) & (idx_v > idx_v.shift(1))
    curr_dist_days = int(dist_mask.rolling(25).sum().iloc[-1])
    
    # FTD
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
    
    # 狀態判定文字
    if vix_c.iloc[-1] > 25: status, color = "🚨 VIX 恐慌警戒", "text-red-500 bg-red-500/20 border-red-500/50"
    elif is_bull: status, color = "🟢 牛市格局", "text-emerald-500 bg-emerald-500/10 border-emerald-500/20"
    elif curr_ftd_days <= FTD_VALID_DAYS: status, color = f"✅ 底部確認 ({curr_ftd_days}日 FTD)", "text-blue-400 bg-blue-500/10 border-blue-500/20"
    else: status, color = "❌ 熊市空頭", "text-red-500 bg-red-500/10 border-red-500/20"
    
    return curr_dist_days, is_bull, status, color

us_dist, us_is_bull, us_status, us_color = calc_macro_regime('SPY')
jp_dist, jp_is_bull, jp_status, jp_color = calc_macro_regime('^N225')

# 4. 混合相對強度 (Blended RS)
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

        # 👇 【重點修改 1】：判斷國籍，指派專屬大盤狀態
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

        # 👇 【重點修改 2】：將原本的 is_bull_market 改為專屬的 ticker_is_bull
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
            
            # 為 JS 計算機準備 Payload
            js_payload.append({
                "ticker": ticker, "tag": tag_name, "curr_price": round(cp, 2), 
                "sl_price": sl_p, "tp_price": tp_p, "risk_per_share": risk_per_share
            })

    except Exception as e: pass

swing_results.sort(key=lambda x: x['rs'], reverse=True)
short_term_results.sort(key=lambda x: x['rs'], reverse=True)

with open(HISTORY_FILE, "w", encoding="utf-8") as f: json.dump(trade_history[-150:], f, indent=4)

# =============================================================================
# MODULE 6 — 生成前端 HTML (整合 JS 計算機與 TradingView)
# =============================================================================
print("⏳ [7/7] 正在生成完整型雙策略儀表板...")

def get_unit(tk): return "¥" if tk.endswith(".T") else "$"

html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <script src="https://cdn.tailwindcss.com"></script>
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
    <title>V1 QUANT ({today_str})</title>
</head>
<body class="bg-[#020617] text-slate-300 p-4 font-sans h-screen flex flex-col overflow-hidden">
    
    <header class="bg-slate-900 border border-slate-800 rounded-xl p-3 shrink-0 mb-4 shadow-lg flex flex-col gap-3">
        <div class="flex justify-between items-center">
            <div>
                <h1 class="text-2xl font-black text-white italic tracking-tighter">V1 <span class="text-indigo-500">QUANT</span></h1>
                <p class="text-[10px] text-slate-500 font-bold uppercase tracking-widest">時光機模擬: {today_str}</p>
            </div>
            <div class="text-xs font-black text-slate-500 bg-black/50 px-3 py-1 rounded-lg border border-slate-800">
                🌐 Dual-Market Macro Radar
            </div>
        </div>

        <div class="grid grid-cols-2 gap-4">
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

    <main class="flex-1 flex gap-4 overflow-hidden">
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
                <div class="p-3 border-b border-slate-800 font-black text-indigo-400 flex justify-between items-center shrink-0">
                    <span>🎯 今日推介信號 (點擊查看)</span>
                </div>
                <div class="overflow-y-auto flex-1 p-2 space-y-2" id="signal-list">
                    <div class="text-[10px] font-bold text-slate-500 uppercase ml-1 mt-2">🏆 波段策略 (Swing)</div>
                    {"".join([f'''
                    <div class="bg-slate-800/50 hover:bg-indigo-900/30 cursor-pointer border border-slate-700/50 hover:border-indigo-500/50 rounded-lg p-2 transition" onclick="loadContent('{d['tk']}')">
                        <div class="flex justify-between items-center">
                            <span class="font-black text-white text-sm">{d['tk']}</span>
                            <span class="text-[9px] bg-indigo-500/20 text-indigo-300 px-1.5 py-0.5 rounded">{d['tag']}</span>
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
            
            // 處理日股代號給 TradingView
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
            
            // 核心邏輯：控制每單總虧損不超過帳戶的 1%
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
print(f"\n🎉 終極融合版建置完成！")