import base64
import hashlib
import hmac
import json
import math
import time
from datetime import datetime

import httpx




API_KEY = '9773cd81-6a39-40c0-ae4c-044f7e7c9a18'
API_SECRET = 'C1D0C6A2D7D0296A302729A9FBF2917E'
PASSPHRASE = '456168855151fhG@'
BASE_URL = "https://www.okx.com"
INST_ID = "ETH-USDT-SWAP"

OKX_DEMO_TRADING = False
LOCAL_SIMULATION = False

simulated_position = None
simulated_entry_order = None
position_sync_miss_count = 0
POSITION_SYNC_GRACE_POLLS = 3
QUERY_FAILED = object()
instrument_rules_cache = None

client = httpx.Client(
    verify=False,
    timeout=15,
    trust_env=False,
)


def get_signature(timestamp, method, request_path, body=""):
    message = timestamp + method + request_path + body
    mac = hmac.new(
        bytes(API_SECRET, encoding="utf8"),
        bytes(message, encoding="utf-8"),
        digestmod=hashlib.sha256,
    )
    return base64.b64encode(mac.digest())


def get_headers(method, request_path, body=""):
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    signature = get_signature(timestamp, method, request_path, body)
    headers = {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": signature.decode("utf-8"),
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
    }
    if OKX_DEMO_TRADING:
        headers["x-simulated-trading"] = "1"
    return headers


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_eth_price():
    url = f"{BASE_URL}/api/v5/market/ticker?instId={INST_ID}"
    response = client.get(url)
    data = response.json()
    return float(data["data"][0]["last"])


def set_leverage(leverage):
    if LOCAL_SIMULATION:
        print(f"[{now_str()}] 模拟模式: 杠杆设置为 {leverage} 倍")
        return

    url = f"{BASE_URL}/api/v5/account/set-leverage"
    body = {
        "instId": INST_ID,
        "lever": str(leverage),
        "mgnMode": "cross",
        "posSide": "short",
    }
    body_str = json.dumps(body)
    headers = get_headers("POST", "/api/v5/account/set-leverage", body_str)
    response = client.post(url, headers=headers, content=body_str)
    result = response.json()
    if result["code"] == "0":
        print(f"[{now_str()}] short 杠杆已设置为 {leverage} 倍")
    else:
        print(f"[{now_str()}] short 设置杠杆失败: {result}")


def calculate_order_quantity(entry_price, leverage, order_usdt_amount):
    rules = get_instrument_rules()
    lot_size = rules["lot_size"]
    min_size = rules["min_size"]
    contract_size = rules["contract_size"]

    contract_face_value = entry_price * contract_size
    target_contract_value = order_usdt_amount * leverage
    raw_qty = target_contract_value / contract_face_value
    if raw_qty < min_size:
        return min_size
    steps = math.floor(raw_qty / lot_size)
    quantized_qty = steps * lot_size
    return max(quantized_qty, min_size)


def calculate_min_margin_for_min_size(entry_price, leverage):
    rules = get_instrument_rules()
    min_size = rules["min_size"]
    contract_size = rules["contract_size"]
    contract_face_value = entry_price * contract_size * min_size
    return contract_face_value / leverage


def format_sz(sz):
    sz_str = f"{sz:.8f}".rstrip("0").rstrip(".")
    return sz_str if sz_str else "0"


def get_instrument_rules():
    global instrument_rules_cache
    if instrument_rules_cache is not None:
        return instrument_rules_cache

    if LOCAL_SIMULATION:
        instrument_rules_cache = {
            "min_size": 1.0,
            "lot_size": 1.0,
            "contract_size": 0.1,
        }
        return instrument_rules_cache

    request_path = f"/api/v5/public/instruments?instType=SWAP&instId={INST_ID}"
    url = f"{BASE_URL}{request_path}"
    response = client.get(url)
    data = response.json()
    if data.get("code") != "0" or not data.get("data"):
        print(f"[{now_str()}] 获取合约规则失败，使用默认规则: {data}")
        instrument_rules_cache = {
            "min_size": 1.0,
            "lot_size": 1.0,
            "contract_size": 0.1,
        }
        return instrument_rules_cache

    instrument = data["data"][0]
    instrument_rules_cache = {
        "min_size": float(instrument.get("minSz", "1")),
        "lot_size": float(instrument.get("lotSz", "1")),
        "contract_size": float(instrument.get("ctVal", "0.1")),
    }
    return instrument_rules_cache


def validate_short_stop_loss_price(entry_price, stop_loss_price):
    if stop_loss_price <= entry_price:
        print(f"[{now_str()}] 空单止损价 {stop_loss_price:.2f} 必须高于挂单价 {entry_price:.2f}")
        return False
    return True


def validate_short_take_profit_price(entry_price, take_profit_price):
    if take_profit_price >= entry_price:
        print(f"[{now_str()}] 空单止盈价 {take_profit_price:.2f} 必须低于挂单价 {entry_price:.2f}")
        return False
    return True


def place_entry_limit_order(entry_price, quantity, stop_loss_price=None, take_profit_price=None):
    global simulated_entry_order

    if LOCAL_SIMULATION:
        simulated_entry_order = {
            "ordId": f"sim_entry_{int(time.time())}",
            "px": float(entry_price),
            "sz": float(quantity),
            "state": "live",
            "slTriggerPx": stop_loss_price,
            "tpTriggerPx": take_profit_price,
        }
        print(f"[{now_str()}] 模拟挂限价开空单: price={entry_price}, qty={quantity}")
        return simulated_entry_order["ordId"]

    url = f"{BASE_URL}/api/v5/trade/order"
    body = {
        "instId": INST_ID,
        "tdMode": "cross",
        "side": "sell",
        "posSide": "short",
        "ordType": "limit",
        "px": str(entry_price),
        "sz": format_sz(quantity),
    }
    if stop_loss_price is not None or take_profit_price is not None:
        attach_algo = {}
        if take_profit_price is not None:
            attach_algo["tpTriggerPx"] = str(take_profit_price)
            attach_algo["tpOrdPx"] = "-1"
        if stop_loss_price is not None:
            attach_algo["slTriggerPx"] = str(stop_loss_price)
            attach_algo["slOrdPx"] = "-1"
        body["attachAlgoOrds"] = [attach_algo]
    body_str = json.dumps(body)
    headers = get_headers("POST", "/api/v5/trade/order", body_str)
    response = client.post(url, headers=headers, content=body_str)
    result = response.json()
    if result["code"] == "0":
        ord_id = result["data"][0]["ordId"]
        print(f"[{now_str()}] 限价挂空单成功: ordId={ord_id}, price={entry_price}, qty={quantity}")
        return ord_id

    print(f"[{now_str()}] 限价挂空单失败: {result}")
    return None


def place_reduce_order(quantity, reduce_only=True):
    if LOCAL_SIMULATION:
        print(f"[{now_str()}] 模拟平空: qty={quantity}")
        return f"sim_close_{int(time.time())}"

    url = f"{BASE_URL}/api/v5/trade/order"
    body = {
        "instId": INST_ID,
        "tdMode": "cross",
        "side": "buy",
        "posSide": "short",
        "ordType": "market",
        "sz": format_sz(quantity),
    }
    if reduce_only:
        body["reduceOnly"] = "true"

    body_str = json.dumps(body)
    headers = get_headers("POST", "/api/v5/trade/order", body_str)
    response = client.post(url, headers=headers, content=body_str)
    result = response.json()
    if result["code"] == "0":
        print(f"[{now_str()}] 平空成功: qty={quantity}")
        return result["data"][0]["ordId"]

    print(f"[{now_str()}] 平空失败: {result}")
    return None


def place_stop_loss(quantity, stop_price):
    if LOCAL_SIMULATION:
        print(f"[{now_str()}] 模拟止损设置: qty={quantity}, price={stop_price}")
        return

    url = f"{BASE_URL}/api/v5/trade/order-algo"
    body = {
        "instId": INST_ID,
        "tdMode": "cross",
        "side": "buy",
        "posSide": "short",
        "ordType": "conditional",
        "sz": format_sz(quantity),
        "slTriggerPx": str(stop_price),
        "slOrdPx": "-1",
    }
    body_str = json.dumps(body)
    headers = get_headers("POST", "/api/v5/trade/order-algo", body_str)
    response = client.post(url, headers=headers, content=body_str)
    result = response.json()
    if result["code"] == "0":
        print(f"[{now_str()}] 止损已设置在价格 {stop_price}")
    else:
        print(f"[{now_str()}] 设置止损失败: {result}")


def place_take_profit(quantity, take_profit_price):
    if LOCAL_SIMULATION:
        print(f"[{now_str()}] 模拟止盈设置: qty={quantity}, price={take_profit_price}")
        return

    url = f"{BASE_URL}/api/v5/trade/order-algo"
    body = {
        "instId": INST_ID,
        "tdMode": "cross",
        "side": "buy",
        "posSide": "short",
        "ordType": "conditional",
        "sz": format_sz(quantity),
        "tpTriggerPx": str(take_profit_price),
        "tpOrdPx": "-1",
    }
    body_str = json.dumps(body)
    headers = get_headers("POST", "/api/v5/trade/order-algo", body_str)
    response = client.post(url, headers=headers, content=body_str)
    result = response.json()
    if result["code"] == "0":
        print(f"[{now_str()}] 止盈已设置在价格 {take_profit_price}")
    else:
        print(f"[{now_str()}] 设置止盈失败: {result}")


def get_positions():
    if LOCAL_SIMULATION:
        return simulated_position

    request_path = f"/api/v5/account/positions?instId={INST_ID}"
    url = f"{BASE_URL}{request_path}"
    headers = get_headers("GET", request_path)
    response = client.get(url, headers=headers)
    data = response.json()
    if data["code"] != "0" or not data["data"]:
        return None

    for pos in data["data"]:
        pos_qty = float(pos.get("pos", "0"))
        if pos_qty <= 0:
            continue
        if pos.get("posSide") != "short":
            continue
        return {
            "pos": pos_qty,
            "avgPx": float(pos["avgPx"]),
            "side": "short",
        }
    return None


def get_pending_entry_order(order_id):
    global simulated_entry_order, simulated_position

    if not order_id:
        return None

    if LOCAL_SIMULATION:
        if simulated_entry_order and simulated_entry_order["ordId"] == order_id:
            current_price = get_eth_price()
            if current_price >= simulated_entry_order["px"]:
                simulated_position = {
                    "pos": simulated_entry_order["sz"],
                    "avgPx": simulated_entry_order["px"],
                    "side": "short",
                }
                simulated_entry_order = None
                return None
            return simulated_entry_order
        return None

    request_path = f"/api/v5/trade/orders-pending?instId={INST_ID}"
    url = f"{BASE_URL}{request_path}"
    headers = get_headers("GET", request_path)
    response = client.get(url, headers=headers)
    data = response.json()
    if data["code"] != "0":
        print(f"[{now_str()}] 查询挂单失败: {data}，本轮不再新增挂单")
        return QUERY_FAILED

    for order in data["data"]:
        if order.get("ordId") == order_id:
            return order
    return None


def get_pending_short_limit_orders():
    global simulated_entry_order

    if LOCAL_SIMULATION:
        if simulated_entry_order:
            return [simulated_entry_order]
        return []

    request_path = f"/api/v5/trade/orders-pending?instId={INST_ID}"
    url = f"{BASE_URL}{request_path}"
    headers = get_headers("GET", request_path)
    response = client.get(url, headers=headers)
    data = response.json()
    if data["code"] != "0":
        print(f"[{now_str()}] 查询挂单失败: {data}，本轮不再新增挂单")
        return QUERY_FAILED

    pending_orders = []
    for order in data["data"]:
        if (
            order.get("side") == "sell"
            and order.get("posSide") == "short"
            and order.get("ordType") == "limit"
        ):
            pending_orders.append(order)
    return pending_orders


def build_ladder_levels(first_value, step_value, count):
    return [first_value + (step_value * i) for i in range(count)]


def almost_equal(a, b, eps=1e-8):
    return abs(float(a) - float(b)) < eps


def get_balance():
    if LOCAL_SIMULATION:
        return 10000.0

    request_path = "/api/v5/account/balance"
    url = f"{BASE_URL}{request_path}"
    headers = get_headers("GET", request_path)
    response = client.get(url, headers=headers)
    data = response.json()
    if data["code"] == "0":
        for bal in data["data"][0]["details"]:
            if bal["ccy"] == "USDT":
                return float(bal["availBal"])
    return 0.0


def trading_strategy(
    n,
    first_entry_price,
    entry_price_step,
    leverage,
    first_stop_loss_price,
    stop_loss_step,
    first_take_profit_price,
    take_profit_step,
    order_usdt_amount,
    interval=10,
):
    global simulated_position, position_sync_miss_count

    entry_prices = build_ladder_levels(first_entry_price, entry_price_step, n)
    stop_loss_prices = build_ladder_levels(first_stop_loss_price, stop_loss_step, n)
    take_profit_prices = build_ladder_levels(first_take_profit_price, take_profit_step, n)

    set_leverage(leverage)
    print(f"[{now_str()}] ETH 实盘限价挂空单程序启动")
    print(
        f"阶梯挂单数量: {n}, 首挂单价: {first_entry_price}, 价格步长: {entry_price_step}, "
        f"首止盈价: {first_take_profit_price}, 止盈步长: {take_profit_step}, "
        f"首止损价: {first_stop_loss_price}, 止损步长: {stop_loss_step}, "
        f"单笔保证金: {order_usdt_amount} USDT, 保证金模式: cross"
    )

    protection_set = False

    while True:
        try:
            current_price = get_eth_price()
            print(f"[{now_str()}] 当前 ETH 价格 = {current_price:.2f}")

            live_position = get_positions()
            if live_position:
                position = live_position
                simulated_position = live_position
                position_sync_miss_count = 0
            elif simulated_position is not None and position_sync_miss_count < POSITION_SYNC_GRACE_POLLS:
                position_sync_miss_count += 1
                position = simulated_position
                print(f"[{now_str()}] 仓位同步延迟，第 {position_sync_miss_count} 次使用本地缓存持仓")
            else:
                position = None
                simulated_position = None
                position_sync_miss_count = 0

            if position:
                print(f"[{now_str()}] 当前持仓: side=short, qty={position['pos']}, avgPx={position['avgPx']}")
                if not protection_set:
                    print(f"[{now_str()}] 已有持仓，跳过阶梯挂单新增（止盈止损将按下单时附带或手动管理）")
                    protection_set = True
                time.sleep(interval)
                continue

            protection_set = False

            pending_entries = get_pending_short_limit_orders()
            if pending_entries is QUERY_FAILED:
                time.sleep(interval)
                continue
            balance = get_balance()
            existing_prices = set()
            for o in pending_entries:
                try:
                    existing_prices.add(float(o.get("px", "0")))
                except Exception:
                    continue

            planned_orders = []
            total_required_margin = 0.0
            rules = get_instrument_rules()
            min_required_margin = min(
                calculate_min_margin_for_min_size(px, leverage) for px in entry_prices
            )
            effective_order_usdt_amount = max(order_usdt_amount, min_required_margin)
            if order_usdt_amount < min_required_margin:
                print(
                    f"[{now_str()}] 输入保证金 {order_usdt_amount:.2f} USDT 低于最小下单需求 "
                    f"{min_required_margin:.4f} USDT，将按最小下单单位自动挂单。"
                )

            for i in range(n):
                entry_price = entry_prices[i]
                stop_loss_price = stop_loss_prices[i]
                take_profit_price = take_profit_prices[i]

                if any(almost_equal(entry_price, p) for p in existing_prices):
                    continue

                if not validate_short_stop_loss_price(entry_price, stop_loss_price):
                    continue
                if not validate_short_take_profit_price(entry_price, take_profit_price):
                    continue

                qty = calculate_order_quantity(entry_price, leverage, effective_order_usdt_amount)
                contract_value = entry_price * rules["contract_size"] * qty
                required_margin = contract_value / leverage
                total_required_margin += required_margin
                planned_orders.append((i + 1, entry_price, stop_loss_price, take_profit_price, qty, required_margin))

            print(
                f"[{now_str()}] 当前无持仓，余额: {balance:.2f} USDT, 计划挂单数: {len(planned_orders)}/{n}, "
                f"预估总保证金: {total_required_margin:.4f} USDT"
            )

            if not planned_orders:
                print(f"[{now_str()}] 无需新增挂单（可能已存在同价挂单或参数校验失败）")
                time.sleep(interval)
                continue

            if balance < total_required_margin:
                print(f"[{now_str()}] 余额不足以一次挂出计划阶梯单，暂不挂单")
                time.sleep(interval)
                continue

            placed_count = 0
            for idx, entry_price, stop_loss_price, take_profit_price, qty, required_margin in planned_orders:
                ord_id = place_entry_limit_order(
                    entry_price,
                    qty,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                )
                if ord_id:
                    placed_count += 1
                    print(
                        f"[{now_str()}] 第{idx}单已挂出: 入场={entry_price}, 止盈={take_profit_price}, "
                        f"止损={stop_loss_price}, qty={qty}, 预估保证金={required_margin:.4f}"
                    )

            print(f"[{now_str()}] 本轮共挂出 {placed_count}/{len(planned_orders)} 个阶梯限价空单")

        except Exception as e:
            print(f"[{now_str()}] Error: {e}")

        time.sleep(interval)


def main():
    #阶梯挂单数量
    n = 4
    #第一次挂单价
    first_entry_price = 2322
    #价格步长
    entry_price_step = 5
    #第一次止盈价
    first_take_profit_price = 2308
    #止盈步长
    take_profit_step = 0
    #第一次止损价
    first_stop_loss_price = 2365
    #止损步长
    stop_loss_step = 2
    #杠杆
    leverage = 100
    #单笔开仓保证金
    order_usdt_amount = 2

    trading_strategy(
        n,
        first_entry_price,
        entry_price_step,
        leverage,
        first_stop_loss_price,
        stop_loss_step,
        first_take_profit_price,
        take_profit_step,
        order_usdt_amount,
        10,
    )


if __name__ == "__main__":
    main()
