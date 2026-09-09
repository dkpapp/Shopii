# graphql.py
#
# Surgically minimal GraphQL queries for Shopify Universal Checkout.
# By strictly requesting only the fields required for calculation and submission,
# we bypass dynamic schema deprecation errors without needing a string sanitizer.

QUERY_PROPOSAL_SHIPPING = """
query Proposal(
  $delivery: DeliveryTermsInput,
  $discounts: DiscountTermsInput,
  $payment: PaymentTermInput,
  $merchandise: MerchandiseTermInput,
  $buyerIdentity: BuyerIdentityTermInput,
  $taxes: TaxTermInput,
  $sessionInput: SessionTokenInput!,
  $checkpointData: String,
  $queueToken: String
) {
  session(sessionInput: $sessionInput) {
    negotiate(
      input: {
        purchaseProposal: {
          delivery: $delivery,
          discounts: $discounts,
          payment: $payment,
          merchandise: $merchandise,
          buyerIdentity: $buyerIdentity,
          taxes: $taxes
        },
        checkpointData: $checkpointData,
        queueToken: $queueToken
      }
    ) {
      __typename
      result {
        ... on NegotiationResultAvailable {
          checkpointData
          queueToken
          sellerProposal {
            delivery {
              __typename
              ... on FilledDeliveryTerms {
                deliveryLines {
                  availableDeliveryStrategies {
                    ... on CompleteDeliveryStrategy {
                      handle
                      amount { ... on MoneyValueConstraint { value { amount currencyCode } } }
                    }
                  }
                  selectedDeliveryStrategy {
                    ... on CompleteDeliveryStrategy { handle }
                  }
                }
              }
              ... on PendingTerms { pollDelay }
            }
            tax {
              __typename
              ... on FilledTaxTerms {
                totalTaxAmount { ... on MoneyValueConstraint { value { amount currencyCode } } }
              }
              ... on PendingTerms { pollDelay }
            }
            runningTotal {
              __typename
              ... on MoneyValueConstraint { value { amount currencyCode } }
            }
            payment {
              __typename
              ... on FilledPaymentTerms {
                availablePaymentLines {
                  paymentMethod {
                    __typename
                    ... on PaymentProvider { paymentMethodIdentifier name extensibilityDisplayName }
                    ... on OffsiteProvider { paymentMethodIdentifier name }
                    ... on CustomOnsiteProvider { paymentMethodIdentifier name }
                    ... on PaypalWalletConfig { name paymentMethodIdentifier }
                    ... on ShopPayWalletConfig { name paymentMethodIdentifier }
                    ... on ApplePayWalletConfig { name paymentMethodIdentifier }
                    ... on GooglePayWalletConfig { name paymentMethodIdentifier }
                    ... on LocalPaymentMethodConfig { paymentMethodIdentifier name displayName }
                    ... on AnyPaymentOnDeliveryMethod { paymentMethodIdentifier name }
                    ... on ManualPaymentMethodConfig { paymentMethodIdentifier name }
                    ... on CustomPaymentMethodConfig { paymentMethodIdentifier name }
                    ... on CustomerCreditCardPaymentMethod { paymentMethodIdentifier name }
                  }
                }
              }
              ... on PendingTerms { pollDelay }
            }
          }
        }
        ... on CheckpointDenied { redirectUrl }
        ... on Throttled { pollAfter queueToken pollUrl }
        ... on NegotiationResultFailed { __typename }
      }
      errors {
        code
        localizedMessage
      }
    }
  }
}
"""

# The delivery proposal is structurally identical to the shipping proposal
QUERY_PROPOSAL_DELIVERY = QUERY_PROPOSAL_SHIPPING

MUTATION_SUBMIT = """
mutation SubmitForCompletion($input: NegotiationInput!, $attemptToken: String!) {
  submitForCompletion(input: $input, attemptToken: $attemptToken) {
    __typename
    ... on SubmitSuccess { 
      receipt { 
        __typename 
        ... on ProcessedReceipt { id } 
        ... on ProcessingReceipt { id } 
        ... on WaitingReceipt { id } 
        ... on ActionRequiredReceipt { id } 
        ... on FailedReceipt { id } 
      } 
    }
    ... on SubmitAlreadyAccepted { 
      receipt { 
        __typename 
        ... on ProcessedReceipt { id } 
        ... on ProcessingReceipt { id } 
        ... on WaitingReceipt { id } 
        ... on ActionRequiredReceipt { id } 
        ... on FailedReceipt { id } 
      } 
    }
    ... on SubmittedForCompletion { 
      receipt { 
        __typename 
        ... on ProcessedReceipt { id } 
        ... on ProcessingReceipt { id } 
        ... on WaitingReceipt { id } 
        ... on ActionRequiredReceipt { id } 
        ... on FailedReceipt { id } 
      } 
    }
    ... on SubmitFailed { reason }
    ... on SubmitRejected {
      errors {
        __typename
        ... on NegotiationError { code localizedMessage }
      }
    }
    ... on Throttled { pollAfter queueToken pollUrl }
    ... on CheckpointDenied { redirectUrl }
  }
}
"""

QUERY_POLL = """
query PollForReceipt($receiptId: ID!, $sessionToken: String!) {
  receipt(receiptId: $receiptId, sessionInput: {sessionToken: $sessionToken}) {
    __typename
    ... on ProcessedReceipt { id }
    ... on ActionRequiredReceipt { id }
    ... on FailedReceipt {
      id
      processingError {
        __typename
        ... on PaymentFailed { code messageUntranslated }
      }
    }
    ... on ProcessingReceipt { id pollDelay }
    ... on WaitingReceipt { id pollDelay }
  }
}
"""