from analysis_bundle import (
    normalize_tencent_kline_date,
    parse_tencent_quote_payload,
)


def test_parse_tencent_quote_payload_normalizes_units():
    fields = [""] * 50
    fields[1] = "测试股份"
    fields[2] = "688008"
    fields[3] = "52.10"
    fields[4] = "50.00"
    fields[5] = "50.50"
    fields[6] = "123456"
    fields[9] = "52.09"
    fields[19] = "52.10"
    fields[30] = "20260730103000"
    fields[31] = "2.10"
    fields[32] = "4.20"
    fields[33] = "53.00"
    fields[34] = "49.80"
    fields[37] = "32100"
    fields[38] = "1.25"
    fields[43] = "6.40"
    fields[44] = "128.5"
    fields[45] = "180.5"
    fields[46] = "3.20"
    raw = 'v_sh688008="' + "~".join(fields) + '";'

    quote = parse_tencent_quote_payload(raw, "688008")

    assert quote["name"] == "测试股份"
    assert quote["current_price"] == 52.1
    assert quote["change_pct"] == 4.2
    assert quote["amount"] == 321_000_000
    assert quote["float_market_cap"] == 12_850_000_000
    assert quote["total_market_cap"] == 18_050_000_000
    assert quote["source"] == "tencent"


def test_parse_tencent_quote_payload_rejects_mismatched_symbol():
    fields = [""] * 50
    fields[2] = "600519"
    raw = 'v_sh600519="' + "~".join(fields) + '";'

    assert parse_tencent_quote_payload(raw, "688008") == {}


def test_normalize_tencent_kline_date_accepts_akshare_format():
    assert normalize_tencent_kline_date("20260730") == "2026-07-30"
    assert normalize_tencent_kline_date("2026-07-30") == "2026-07-30"
    assert normalize_tencent_kline_date("") == ""
