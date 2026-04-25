import base64
import hashlib
import hmac
import json
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
    contract_face_value = entry_price * 0.1
    target_contract_value = order_usdt_amount * leverage
    qty = int(target_contract_value / contract_face_value)
    return max(qty, 1)


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


def place_entry_limit_order(entry_price, quantity):
    global simulated_entry_order

    if LOCAL_SIMULATION:
        simulated_entry_order = {
            "ordId": f"sim_entry_{int(time.time())}",
            "px": float(entry_price),
            "sz": float(quantity),
            "state": "live",
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
        "sz": str(quantity),
    }
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
        "sz": str(quantity),
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
        "sz": str(quantity),
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
        "sz": str(quantity),
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


def get_same_pending_entry_order(entry_price):
    global simulated_entry_order

    if LOCAL_SIMULATION:
        if not simulated_entry_order:
            return None
        if abs(float(simulated_entry_order.get("px", 0)) - float(entry_price)) < 1e-8:
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
        if (
            order.get("side") == "sell"
            and order.get("posSide") == "short"
            and order.get("ordType") == "limit"
            and abs(float(order.get("px", "0")) - float(entry_price)) < 1e-8
        ):
            return order
    return None


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


def trading_strategy(entry_price, take_profit_price, leverage, stop_loss_price, order_usdt_amount, interval=10):
    global simulated_position, position_sync_miss_count

    set_leverage(leverage)
    print(f"[{now_str()}] ETH 实盘限价挂空单程序启动")
    print(
        f"挂单价: {entry_price}, 止盈价: {take_profit_price}, 止损价: {stop_loss_price}, "
        f"开仓保证金: {order_usdt_amount} USDT, 保证金模式: cross"
    )

    active_entry_order_id = None
    startup_pending_entry = get_same_pending_entry_order(entry_price)
    if startup_pending_entry is QUERY_FAILED:
        print(f"[{now_str()}] 启动时挂单查询失败，将在循环中继续重试")
    elif startup_pending_entry:
        active_entry_order_id = startup_pending_entry.get("ordId")
        print(
            f"[{now_str()}] 启动检测到同价挂单: ordId={startup_pending_entry.get('ordId')}, "
            f"price={startup_pending_entry.get('px')}, qty={startup_pending_entry.get('sz')}"
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
                active_entry_order_id = None
                if not protection_set:
                    place_stop_loss(position["pos"], stop_loss_price)
                    place_take_profit(position["pos"], take_profit_price)
                    protection_set = True
                time.sleep(interval)
                continue

            protection_set = False

            if not active_entry_order_id:
                same_pending_entry = get_same_pending_entry_order(entry_price)
                if same_pending_entry is QUERY_FAILED:
                    time.sleep(interval)
                    continue
                if same_pending_entry:
                    active_entry_order_id = same_pending_entry.get("ordId")
                    print(
                        f"[{now_str()}] 检测到同价挂单: ordId={same_pending_entry.get('ordId')}, "
                        f"price={same_pending_entry.get('px')}, qty={same_pending_entry.get('sz')}"
                    )
                    time.sleep(interval)
                    continue

            pending_entry = get_pending_entry_order(active_entry_order_id)
            if pending_entry is QUERY_FAILED:
                time.sleep(interval)
                continue
            if pending_entry:
                print(
                    f"[{now_str()}] 当前有未成交挂单: ordId={pending_entry['ordId']}, "
                    f"price={pending_entry.get('px')}, qty={pending_entry.get('sz')}"
                )
                time.sleep(interval)
                continue

            active_entry_order_id = None

            qty = calculate_order_quantity(entry_price, leverage, order_usdt_amount)
            balance = get_balance()
            contract_value = entry_price * 0.1 * qty
            required_margin = contract_value / leverage
            print(
                f"[{now_str()}] 当前无持仓且无挂单，当前余额: {balance:.2f} USDT, "
                f"所需保证金: {required_margin:.2f} USDT, 本次挂单张数: {qty}"
            )

            if (
                balance >= required_margin
                and validate_short_stop_loss_price(entry_price, stop_loss_price)
                and validate_short_take_profit_price(entry_price, take_profit_price)
            ):
                ord_id = place_entry_limit_order(entry_price, qty)
                if ord_id:
                    active_entry_order_id = ord_id
                    print(f"[{now_str()}] 已挂新限价空单，等待成交后再挂下一单")
            else:
                print(f"[{now_str()}] 可用余额不足，或止损/止盈价格无效，暂不挂单")

        except Exception as e:
            print(f"[{now_str()}] Error: {e}")

        time.sleep(interval)


def main():
    entry_price = 2325
    take_profit_price = 2312
    stop_loss_price = 2365
    leverage = 100
    order_usdt_amount = 4

    trading_strategy(
        entry_price,
        take_profit_price,
        leverage,
        stop_loss_price,
        order_usdt_amount,
        10,
    )


if __name__ == "__main__":
    main()
