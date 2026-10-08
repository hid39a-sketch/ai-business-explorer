//+------------------------------------------------------------------+
//| KuzureSanzonEA.mq5                                               |
//| 崩れ三尊（高値が①→②→③と切り下がる三尊）の右肩だけを売る EA   |
//|                                                                  |
//| 流れ: 上位足のスラスト（急落）→ 執行足のヨコヨコ →              |
//|       崩れ三尊①②③ → ③が25-5MAで止められる →                  |
//|       安値（ネック）を抜けて DMA3x3 割れの陰線で売り            |
//| 売り専用。ルールの出典は同じフォルダの README.md を参照。        |
//+------------------------------------------------------------------+
#property copyright "hid39a-sketch"
#property version   "1.00"
#property description "崩れ三尊①②③の右肩を売るだけの EA（売り専用）"

#include <Trade\Trade.mqh>

enum ENUM_TP_MODE
  {
   TP_RR = 0, // 損切り幅の倍数（RR）
   TP_FE = 1  // フィボナッチ・エクスパンション（FE）
  };

input group "時間足"
input ENUM_TIMEFRAMES InpExecTF  = PERIOD_M15; // 執行足（崩れ三尊を探す足）
input ENUM_TIMEFRAMES InpUpperTF = PERIOD_H1;  // 上位足（スラストを判定する足）

input group "スラスト（お告げ）"
input double InpThrustPips      = 50.0;  // 上位足の高値からの下落幅（pips）
input int    InpThrustLookback  = 24;    // 上位足で高値を探す本数
input bool   InpRequireWakuwaku = false; // 上位足で陰線実体の DMA3x3 割れ（ワクワクさん）を必須にする

input group "崩れ三尊①②③"
input int    InpPivotBars     = 2;   // 山とみなす左右の本数
input int    InpPatternBars   = 60;  // ①を探す最大の本数（執行足）
input double InpEqualTolPips  = 3.0; // ②が①をこの幅（pips）までなら上回ってもよい
input double InpMinStepPips   = 0.0; // ③が②より最低何 pips 低いか
input double InpMaTouchPips   = 3.0; // ③の高値が 25-5MA からこの幅（pips）以内まで届いていること
input int    InpMaxBarsAfterR = 8;   // ③からエントリーまでの最大の本数
input bool   InpRequireNeckBreak = true; // ①〜③の間の安値（ネック）を終値で割ることを必須にする

input group "移動平均線（ディナポリ）"
input int    InpFastPeriod   = 3;   // DMA3x3 の期間
input int    InpFastShift    = 3;   // DMA3x3 のずらし本数
input int    InpSlowPeriod   = 25;  // DMA25x5 の期間
input int    InpSlowShift    = 5;   // DMA25x5 のずらし本数
input int    InpSlopeBars    = 5;   // 25-5MA の下向きを比べる本数
input double InpMinSlopePips = 1.0; // 下向きとみなす最低の傾き（pips）。MA が横ばいの相場を避ける

input group "決済"
input double       InpSLBufferPips    = 2.0;   // 損切り＝③の高値＋この幅（pips）
input double       InpMaxSLPips       = 40.0;  // 損切り幅がこれより大きければ見送る（pips）
input ENUM_TP_MODE InpTPMode          = TP_RR; // 利確の決め方
input double       InpRR              = 1.0;   // RR のときの倍率（1.0＝損切りと同じ幅）
input double       InpFERatio         = 1.0;   // FE のときの比率（③から ①→ネックの値幅×比率）
input bool         InpExitOnFastAbove = false; // 確定足が DMA3x3 の上で終わったら決済する

input group "資金管理・その他"
input double InpRiskPercent   = 1.0;      // 1回の損失を残高の何%にするか
input double InpFixedLots     = 0.0;      // 0より大きければ固定ロット（リスク%は使わない）
input double InpMaxSpreadPips = 3.0;      // これより広いスプレッドでは入らない（pips）
input int    InpStartHour     = 0;        // 取引開始の時（サーバー時間）
input int    InpEndHour       = 24;       // 取引終了の時（サーバー時間）
input double InpPipSize       = 0.0;      // 1pip の大きさ。0なら自動（3桁・5桁は10ポイント）
input long   InpMagic         = 20251205; // マジックナンバー
input bool   InpDrawPattern   = true;     // エントリーした①②③とネックをチャートに描く

CTrade   g_trade;
int      g_hExecFast  = INVALID_HANDLE;
int      g_hExecSlow  = INVALID_HANDLE;
int      g_hUpperFast = INVALID_HANDLE;
double   g_pip        = 0.0;
datetime g_lastBar    = 0;
datetime g_lastR3     = 0; // 同じ右肩で2回入らないための記録

const string OBJ_PREFIX = "KSZ_";

//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpPipSize > 0.0)
      g_pip = InpPipSize;
   else
      g_pip = (_Digits == 3 || _Digits == 5) ? 10.0 * _Point : _Point;

   // ずらしは自分で読む位置で表すので、iMA 自体は ma_shift=0 で作る
   g_hExecFast  = iMA(_Symbol, InpExecTF,  InpFastPeriod, 0, MODE_SMA, PRICE_CLOSE);
   g_hExecSlow  = iMA(_Symbol, InpExecTF,  InpSlowPeriod, 0, MODE_SMA, PRICE_CLOSE);
   g_hUpperFast = iMA(_Symbol, InpUpperTF, InpFastPeriod, 0, MODE_SMA, PRICE_CLOSE);
   if(g_hExecFast == INVALID_HANDLE || g_hExecSlow == INVALID_HANDLE || g_hUpperFast == INVALID_HANDLE)
     {
      Print("移動平均線を作れませんでした: ", GetLastError());
      return INIT_FAILED;
     }
   if(InpPivotBars < 1 || InpPatternBars <= InpPivotBars * 3)
     {
      Print("InpPivotBars か InpPatternBars の値が不正です");
      return INIT_PARAMETERS_INCORRECT;
     }

   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   g_trade.SetDeviationInPoints(10);
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_hExecFast != INVALID_HANDLE)
      IndicatorRelease(g_hExecFast);
   if(g_hExecSlow != INVALID_HANDLE)
      IndicatorRelease(g_hExecSlow);
   if(g_hUpperFast != INVALID_HANDLE)
      IndicatorRelease(g_hUpperFast);
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   // 執行足の新しい足ができたときだけ判定する（確定足で判断する）
   datetime barTime = iTime(_Symbol, InpExecTF, 0);
   if(barTime == 0 || barTime == g_lastBar)
      return;
   g_lastBar = barTime;

   ulong ticket = FindPosition();
   if(ticket != 0)
     {
      if(InpExitOnFastAbove)
         CheckExit(ticket);
      return;
     }
   CheckEntry();
  }

//+------------------------------------------------------------------+
//| この EA の売りポジションを探す（なければ 0）                     |
//+------------------------------------------------------------------+
ulong FindPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == InpMagic)
         return ticket;
     }
   return 0;
  }

//+------------------------------------------------------------------+
//| SMA の値を読む。ずらし（DMA）は shift に足して表す               |
//+------------------------------------------------------------------+
double MaAt(const int handle, const int shift)
  {
   double buf[1];
   if(CopyBuffer(handle, 0, shift, 1, buf) != 1)
      return EMPTY_VALUE;
   return buf[0];
  }

//+------------------------------------------------------------------+
//| r（新しい順）の i 本目が山か。左右 InpPivotBars 本より高いこと   |
//+------------------------------------------------------------------+
bool IsSwingHigh(const MqlRates &r[], const int i)
  {
   for(int k = 1; k <= InpPivotBars; k++)
     {
      if(r[i].high <= r[i - k].high) // 新しい側は同値も不可
         return false;
      if(r[i].high < r[i + k].high)  // 古い側は同値なら可
         return false;
     }
   return true;
  }

//+------------------------------------------------------------------+
bool InTradingHours()
  {
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(InpStartHour <= InpEndHour)
      return dt.hour >= InpStartHour && dt.hour < InpEndHour;
   return dt.hour >= InpStartHour || dt.hour < InpEndHour; // 日付をまたぐ設定
  }

//+------------------------------------------------------------------+
//| 上位足のスラスト。高値から InpThrustPips 以上下げていれば、     |
//| その高値の足の時刻を返す（なければ 0）                           |
//+------------------------------------------------------------------+
datetime FindThrustTop()
  {
   MqlRates u[];
   ArraySetAsSeries(u, true);
   int n = CopyRates(_Symbol, InpUpperTF, 0, InpThrustLookback, u);
   if(n < 2)
      return 0;

   int top = 0;
   for(int i = 1; i < n; i++)
      if(u[i].high > u[top].high)
         top = i;

   double lowAfter = u[top].low;
   for(int i = 0; i < top; i++)
      lowAfter = MathMin(lowAfter, u[i].low);
   if(u[top].high - lowAfter < InpThrustPips * g_pip)
      return 0;

   if(InpRequireWakuwaku)
     {
      // 高値のあと、陰線の実体で DMA3x3 を上から下に割った確定足があること
      bool found = false;
      for(int i = 1; i <= top && !found; i++)
        {
         double fast = MaAt(g_hUpperFast, i + InpFastShift);
         if(fast == EMPTY_VALUE)
            return 0;
         found = (u[i].open > fast && u[i].close < fast);
        }
      if(!found)
         return 0;
     }
   return u[top].time;
  }

//+------------------------------------------------------------------+
void CheckEntry()
  {
   if(!InTradingHours())
      return;

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(ask - bid > InpMaxSpreadPips * g_pip)
      return;

   // 1) 上位足のスラスト
   datetime thrustTop = FindThrustTop();
   if(thrustTop == 0)
      return;

   // 2) 執行足の直近の山を新しい順に3つ（③②①）
   int need = InpPatternBars + InpPivotBars + 2;
   MqlRates r[];
   ArraySetAsSeries(r, true);
   if(CopyRates(_Symbol, InpExecTF, 0, need, r) < need)
      return;

   int pivots[3];
   int found = 0;
   for(int i = InpPivotBars + 1; i < need - InpPivotBars && found < 3; i++)
      if(IsSwingHigh(r, i))
         pivots[found++] = i;
   if(found < 3)
      return;

   int i3 = pivots[0], i2 = pivots[1], i1 = pivots[2];
   if(i1 > InpPatternBars || i3 - 1 > InpMaxBarsAfterR)
      return;
   if(r[i3].time <= g_lastR3)
      return;
   if(r[i1].time < thrustTop) // ①はスラストの高値より後
      return;

   double h1 = r[i1].high, h2 = r[i2].high, h3 = r[i3].high;
   if(h2 > h1 + InpEqualTolPips * g_pip)
      return;
   if(h2 - h3 < MathMax(InpMinStepPips * g_pip, _Point)) // ③は②より低い（右肩下がり）
      return;
   for(int k = 1; k < i3; k++) // ③のあとで③を超えていない
      if(r[k].high > h3)
         return;

   // 3) ③が下向きの 25-5MA に届いて止められた（グランビル）
   double slowR3   = MaAt(g_hExecSlow, i3 + InpSlowShift);
   double slow1    = MaAt(g_hExecSlow, 1 + InpSlowShift);
   double slowPast = MaAt(g_hExecSlow, 1 + InpSlopeBars + InpSlowShift);
   double fast1    = MaAt(g_hExecFast, 1 + InpFastShift);
   if(slowR3 == EMPTY_VALUE || slow1 == EMPTY_VALUE || slowPast == EMPTY_VALUE || fast1 == EMPTY_VALUE)
      return;
   if(slowPast - slow1 < InpMinSlopePips * g_pip)
      return;
   if(h3 < slowR3 - InpMaTouchPips * g_pip)
      return;

   // 4) シグナル足（確定足）: 陰線で、25-5MA と DMA3x3 の下で終わる
   double c1 = r[1].close;
   if(c1 >= r[1].open || c1 >= slow1 || c1 >= fast1)
      return;

   // 5) ネック（①〜③の間の安値）を、この足で初めて割った
   double neck = DBL_MAX;
   for(int k = i3 + 1; k < i1; k++)
      neck = MathMin(neck, r[k].low);
   if(InpRequireNeckBreak)
     {
      if(c1 >= neck)
         return;
      for(int k = 2; k < i3; k++)
         if(r[k].close < neck)
            return; // すでに割れていた（乗り遅れ）
     }

   // 6) 損切り・利確・ロット
   double minDist = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   double sl = NormalizeDouble(h3 + InpSLBufferPips * g_pip + (ask - bid), _Digits);
   double slDist = sl - bid;
   if(slDist <= minDist || slDist > InpMaxSLPips * g_pip)
      return;

   double tp;
   if(InpTPMode == TP_FE)
      tp = h3 - (h1 - neck) * InpFERatio;
   else
      tp = bid - slDist * InpRR;
   tp = NormalizeDouble(tp, _Digits);
   if(bid - tp <= minDist)
      return;

   double lots = CalcLots(slDist);
   if(lots <= 0.0)
     {
      Print("ロットが最小ロットに届かないため見送りました");
      return;
     }

   if(g_trade.Sell(lots, _Symbol, 0.0, sl, tp, "KuzureSanzon"))
     {
      g_lastR3 = r[i3].time;
      if(InpDrawPattern)
         DrawPattern(r, i1, i2, i3, neck);
     }
   else
      Print("売り注文に失敗しました: ", g_trade.ResultRetcode(), " ", g_trade.ResultRetcodeDescription());
  }

//+------------------------------------------------------------------+
//| 確定足が DMA3x3 の上で終わったら決済（任意）                     |
//+------------------------------------------------------------------+
void CheckExit(const ulong ticket)
  {
   double fast1 = MaAt(g_hExecFast, 1 + InpFastShift);
   double c1 = iClose(_Symbol, InpExecTF, 1);
   if(fast1 == EMPTY_VALUE || c1 == 0.0)
      return;
   if(c1 > fast1)
      g_trade.PositionClose(ticket);
  }

//+------------------------------------------------------------------+
//| 損失が残高の InpRiskPercent% になるロット                        |
//+------------------------------------------------------------------+
double CalcLots(const double slDist)
  {
   if(InpFixedLots > 0.0)
      return NormalizeLots(InpFixedLots);

   double risk = AccountInfoDouble(ACCOUNT_BALANCE) * InpRiskPercent / 100.0;
   double bid  = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double lossPerLot = 0.0;
   if(!OrderCalcProfit(ORDER_TYPE_SELL, _Symbol, 1.0, bid, bid + slDist, lossPerLot) || lossPerLot >= 0.0)
      return 0.0;
   return NormalizeLots(risk / -lossPerLot);
  }

//+------------------------------------------------------------------+
double NormalizeLots(double lots)
  {
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(step > 0.0)
      lots = MathFloor(lots / step) * step;
   if(lots < vmin)
      return 0.0;
   return MathMin(lots, vmax);
  }

//+------------------------------------------------------------------+
//| エントリーした①②③とネックを描く（検証用）                      |
//+------------------------------------------------------------------+
void DrawPattern(const MqlRates &r[], const int i1, const int i2, const int i3, const double neck)
  {
   string id = OBJ_PREFIX + TimeToString(r[i3].time, TIME_DATE | TIME_MINUTES) + "_";
   int idx[3];
   idx[0] = i1;
   idx[1] = i2;
   idx[2] = i3;
   string labels[3] = {"1", "2", "3"};
   for(int j = 0; j < 3; j++)
     {
      string name = id + labels[j];
      if(ObjectCreate(0, name, OBJ_TEXT, 0, r[idx[j]].time, r[idx[j]].high))
        {
         ObjectSetString(0, name, OBJPROP_TEXT, labels[j]);
         ObjectSetInteger(0, name, OBJPROP_COLOR, clrRed);
         ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_LOWER);
         ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 12);
        }
     }
   string neckName = id + "neck";
   if(ObjectCreate(0, neckName, OBJ_TREND, 0, r[i1].time, neck, r[1].time, neck))
     {
      ObjectSetInteger(0, neckName, OBJPROP_COLOR, clrDeepSkyBlue);
      ObjectSetInteger(0, neckName, OBJPROP_WIDTH, 2);
      ObjectSetInteger(0, neckName, OBJPROP_RAY_RIGHT, false);
     }
  }
//+------------------------------------------------------------------+
