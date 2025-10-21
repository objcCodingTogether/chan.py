from typing import Iterable, Optional

from Common.CEnum import AUTYPE, DATA_FIELD, KL_TYPE
from Common.CTime import CTime
from Common.func_util import kltype_lt_day, str2float
from KLine.KLine_Unit import CKLine_Unit

from .CommonStockAPI import CCommonStockApi


def _parse_time_column(inp: str) -> CTime:
    # Support: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM', 'YYYY-MM-DD HH:MM:SS',
    # and compact formats like 'YYYYMMDDHHMMSS' (just in case)
    s = inp.strip()
    if len(s) == 10 and s[4] == '-' and s[7] == '-':
        year = int(s[:4])
        month = int(s[5:7])
        day = int(s[8:10])
        hour = minute = 0
        return CTime(year, month, day, hour, minute)
    if len(s) >= 16 and s[4] == '-' and s[7] == '-' and s[10] == ' ':
        # 'YYYY-MM-DD HH:MM[:SS]'
        year = int(s[:4])
        month = int(s[5:7])
        day = int(s[8:10])
        hour = int(s[11:13])
        minute = int(s[14:16])
        return CTime(year, month, day, hour, minute)
    if len(s) == 14 and s.isdigit():
        # 'YYYYMMDDHHMMSS'
        year = int(s[:4])
        month = int(s[4:6])
        day = int(s[6:8])
        hour = int(s[8:10])
        minute = int(s[10:12])
        return CTime(year, month, day, hour, minute)
    if len(s) == 8 and s.isdigit():
        # 'YYYYMMDD'
        year = int(s[:4])
        month = int(s[4:6])
        day = int(s[6:8])
        hour = minute = 0
        return CTime(year, month, day, hour, minute)
    raise Exception(f"unknown time column from akshare: {inp}")


def _normalize_number(v) -> float:
    # Convert common akshare outputs (which may be numeric or strings with '%') to float
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        return 0.0
    s = str(v).strip()
    if s.endswith('%'):
        s = s[:-1]
    return str2float(s)


def _make_item_dict(data, column_name):
    # data[0] is time string; others are numeric-like
    res = {}
    for i in range(len(data)):
        if i == 0:
            res[column_name[i]] = _parse_time_column(str(data[i]))
        else:
            res[column_name[i]] = _normalize_number(data[i])
    return res


def _get_columns_from_fields(fields: str):
    mapping = {
        "time": DATA_FIELD.FIELD_TIME,
        "open": DATA_FIELD.FIELD_OPEN,
        "high": DATA_FIELD.FIELD_HIGH,
        "low": DATA_FIELD.FIELD_LOW,
        "close": DATA_FIELD.FIELD_CLOSE,
        "volume": DATA_FIELD.FIELD_VOLUME,
        "amount": DATA_FIELD.FIELD_TURNOVER,
        "turn": DATA_FIELD.FIELD_TURNRATE,
    }
    return [mapping[x] for x in fields.split(",")]


def _to_ak_adjust(autype: AUTYPE) -> Optional[str]:
    # akshare adjust: None, 'qfq', 'hfq'
    if autype == AUTYPE.QFQ:
        return "qfq"
    if autype == AUTYPE.HFQ:
        return "hfq"
    return None


def _norm_code_minute(code: str) -> str:
    # ak.stock_zh_a_minute requires like 'sh600000' / 'sz000001'
    code = code.strip()
    if '.' in code:
        pre, num = code.split('.', 1)
        pre = pre.lower()
        if pre in ("sh", "sz"):
            return f"{pre}{num}"
    # Guess by first digit
    if code.startswith(('6', '9')):
        return f"sh{code}"
    return f"sz{code}"


def _norm_code_daily(code: str) -> str:
    # ak.stock_zh_a_hist typically accepts 6-digit code without prefix
    return code.split('.', 1)[1] if '.' in code else code


class CAkShare(CCommonStockApi):
    is_connect = None

    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=AUTYPE.QFQ):
        super(CAkShare, self).__init__(code, k_type, begin_date, end_date, autype)

    def get_kl_data(self) -> Iterable[CKLine_Unit]:
        try:
            import akshare as ak  # type: ignore
        except Exception as e:
            raise Exception("akshare is required for DataAPI.AkShareAPI. Please install it: pip install akshare") from e

        if kltype_lt_day(self.k_type):
            # minute-level: only time, open, high, low, close
            fields = "time,open,high,low,close"
            cols = _get_columns_from_fields(fields)
            period = self.__convert_type_minute()
            symbol = _norm_code_minute(self.code)
            # Prefer EM minute API with start/end support if available
            df = None
            err = None
            try:
                # stock_zh_a_hist_min_em supports start/end range
                start_dt = self.__format_datetime(self.begin_date, start_of_day=True)
                end_dt = self.__format_datetime(self.end_date, start_of_day=False)
                df = ak.stock_zh_a_hist_min_em(symbol=_norm_code_daily(self.code), period=period, adjust=_to_ak_adjust(self.autype) or "",
                                               start_date=start_dt, end_date=end_dt)
            except Exception as e:
                err = e
                df = None
            if df is None or df.empty:
                # fallback to minute API without range filter
                try:
                    df = ak.stock_zh_a_minute(symbol=symbol, period=period, adjust=_to_ak_adjust(self.autype))
                except Exception as e:
                    if err is not None:
                        raise err
                    raise e

            # Expect columns: 时间, 开盘, 最高, 最低, 收盘 or similar
            # Normalize by common Chinese headers
            time_col = next((c for c in df.columns if str(c).startswith('时') or str(c).startswith('日')), None)
            open_col = next((c for c in df.columns if str(c).startswith('开')), None)
            high_col = next((c for c in df.columns if str(c).startswith('最') and '高' in str(c)), None)
            low_col = next((c for c in df.columns if str(c).startswith('最') and '低' in str(c)), None)
            close_col = next((c for c in df.columns if str(c).startswith('收')), None)
            if not all([time_col, open_col, high_col, low_col, close_col]):
                # Try English headers fallback
                time_col = time_col or 'time'
                open_col = open_col or 'open'
                high_col = high_col or 'high'
                low_col = low_col or 'low'
                close_col = close_col or 'close'

            for _, row in df.iterrows():
                data = [row[time_col], row[open_col], row[high_col], row[low_col], row[close_col]]
                yield CKLine_Unit(_make_item_dict(data, cols))
        else:
            # day/week/month: include volume/amount/turn
            fields = "time,open,high,low,close,volume,amount,turn"
            cols = _get_columns_from_fields(fields)
            period = self.__convert_type_day()
            code = _norm_code_daily(self.code)
            start_d = self.__format_date(self.begin_date)
            end_d = self.__format_date(self.end_date)
            df = None
            try:
                df = ak.stock_zh_a_hist(symbol=code, period=period, start_date=start_d, end_date=end_d,
                                        adjust=_to_ak_adjust(self.autype) or "")
            except Exception:
                # Try index API if stock API fails
                try:
                    df = ak.index_zh_a_hist(symbol=code, period=period, start_date=start_d, end_date=end_d)
                except Exception as e2:
                    raise e2

            # Normalize headers
            # Common headers: 日期, 开盘, 最高, 最低, 收盘, 成交量, 成交额, 换手率
            time_col = next((c for c in df.columns if str(c).startswith('日')), None)
            open_col = next((c for c in df.columns if str(c).startswith('开')), None)
            high_col = next((c for c in df.columns if str(c).startswith('最') and '高' in str(c)), None)
            low_col = next((c for c in df.columns if str(c).startswith('最') and '低' in str(c)), None)
            close_col = next((c for c in df.columns if str(c).startswith('收')), None)
            vol_col = next((c for c in df.columns if str(c).startswith('成') and '量' in str(c)), None)
            amt_col = next((c for c in df.columns if str(c).startswith('成') and '额' in str(c)), None)
            turn_col = next((c for c in df.columns if '换手' in str(c)), None)

            # Fallback English if needed
            time_col = time_col or 'date'
            open_col = open_col or 'open'
            high_col = high_col or 'high'
            low_col = low_col or 'low'
            close_col = close_col or 'close'
            vol_col = vol_col or 'volume'
            amt_col = amt_col or 'amount'
            turn_col = turn_col or 'turnover_rate'

            for _, row in df.iterrows():
                data = [
                    row[time_col],
                    row[open_col],
                    row[high_col],
                    row[low_col],
                    row[close_col],
                    row.get(vol_col, 0.0),
                    row.get(amt_col, 0.0),
                    row.get(turn_col, 0.0),
                ]
                yield CKLine_Unit(_make_item_dict(data, cols))

    def SetBasciInfo(self):
        # Best-effort: set name as code and mark stock as True by default
        # Users can refine this or extend in their own environment
        self.name = self.code
        self.is_stock = True

    @classmethod
    def do_init(cls):
        pass

    @classmethod
    def do_close(cls):
        pass

    def __convert_type_day(self) -> str:
        mapping = {
            KL_TYPE.K_DAY: 'daily',
            KL_TYPE.K_WEEK: 'weekly',
            KL_TYPE.K_MON: 'monthly',
        }
        return mapping[self.k_type]

    def __convert_type_minute(self) -> str:
        mapping = {
            KL_TYPE.K_1M: '1',
            KL_TYPE.K_5M: '5',
            KL_TYPE.K_15M: '15',
            KL_TYPE.K_30M: '30',
            KL_TYPE.K_60M: '60',
        }
        return mapping[self.k_type]

    @staticmethod
    def __format_date(d: Optional[str]) -> str:
        if not d:
            return ''
        s = str(d).replace('-', '').replace('/', '')
        # Ensure YYYYMMDD
        if len(s) >= 8:
            return s[:8]
        return s

    @staticmethod
    def __format_datetime(d: Optional[str], start_of_day: bool) -> str:
        if not d:
            return ''
        # Accept 'YYYY-MM-DD' or 'YYYY/MM/DD' or 'YYYYMMDD'
        s = str(d).strip()
        if len(s) == 10 and (s[4] == '-' or s[4] == '/'):
            date_part = s.replace('/', '-')
        elif len(s) == 8 and s.isdigit():
            date_part = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        else:
            # already contains time
            return s
        return f"{date_part} 00:00:00" if start_of_day else f"{date_part} 23:59:59"
