""" Braintree payment processing. """
from __future__ import absolute_import, unicode_literals
import logging
from urlparse import urljoin

import braintree
from braintree.attribute_getter import AttributeGetter
from django.conf import settings
from django.core.urlresolvers import reverse
from oscar.apps.payment.exceptions import GatewayError
from oscar.core.loading import get_model

from ecommerce.extensions.order.constants import PaymentEventTypeName
from ecommerce.extensions.payment.processors import BasePaymentProcessor

logger = logging.getLogger(__name__)

PaymentEvent = get_model('order', 'PaymentEvent')
PaymentEventType = get_model('order', 'PaymentEventType')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')
ProductClass = get_model('catalogue', 'ProductClass')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')


class Braintree(BasePaymentProcessor):
    NAME = 'braintree'

    def __init__(self):
        """
        Constructs a new instance of the Braintree processor.

        Raises:
            KeyError: If no settings configured for this payment processor
            AttributeError: If ECOMMERCE_URL_ROOT setting is not set.
        """
        configuration = self.configuration
        self.enviroment_mode = configuration['mode']
        self.merchant_id = configuration['merchant_id']
        self.public_key = configuration['public_key']
        self.private_key = configuration['private_key']
        self.receipt_page_url = configuration['receipt_page_url']
        self.ecommerce_url_root = settings.ECOMMERCE_URL_ROOT

        if self.enviroment_mode == 'production':
            self.enviroment_mode_class = braintree.Environment.Production
        else:
            self.enviroment_mode_class = braintree.Environment.Sandbox

        braintree.Configuration.configure(self.enviroment_mode_class,
                                          merchant_id=self.merchant_id,
                                          public_key=self.public_key,
                                          private_key=self.private_key)

    def get_transaction_parameters(self, basket, request=None):
        return {
            'payment_page_url': urljoin(self.ecommerce_url_root,
                                        reverse('braintree_checkout')),
            'basket_id': basket.pk,
            'merchant_id': self.merchant_id,
            'client_token': self.generate_client_token(request.user),
            'mode': self.enviroment_mode
        }

    def handle_processor_response(self, nonce, basket=None):
        return self.create_transaction(nonce, basket)

    def _record_refund(self, source, amount):
        transaction_id = source.reference
        order = source.order

        source.refund(amount, reference=transaction_id)

        event_type, __ = PaymentEventType.objects.get_or_create(
            name=PaymentEventTypeName.REFUNDED
        )
        PaymentEvent.objects.create(event_type=event_type,
                                    order=order, amount=amount,
                                    reference=transaction_id,
                                    processor_name=self.NAME)

    def issue_credit(self, source, amount, currency):
        transaction_id = source.reference

        # Try voiding first
        result = braintree.Transaction.void(transaction_id)
        if result.is_success:
            # Since the transaction is voided, the entire
            # allocation should be refunded. No partial refunds.
            self._record_refund(source, source.amount_allocated)
            return
        else:
            logger.info('Failed to void Braintree transaction [%s]. '
                        'Attempting a refund. Error message was: %s',
                        transaction_id, result.message)

        result = braintree.Transaction.refund(transaction_id, amount)
        if result.is_success:
            self._record_refund(source, amount)
        else:
            # TODO Log deep errors
            logger.error('Failed to refund Braintree transaction [%s]: %s',
                         transaction_id, result.message)
            raise GatewayError

    def generate_client_token(self, user):
        params = {}
        customer = None
        customer_id = user.get_username()

        try:
            customer = braintree.Customer.find(customer_id)
            logger.debug('Found existing Braintree customer [%s].',
                         customer_id)
            braintree.Customer.update(customer_id, {
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                # custom fields need to be added in braintree admin panel
                # before we can use it here. More info
                # https://developers.braintreepayments.com/reference/request/customer/create/python
                # 'custom_fields': {
                #     'full_name': user.get_full_name()
                # }
            })
        except braintree.exceptions.not_found_error.NotFoundError:
            result = braintree.Customer.create({
                'id': customer_id,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                # 'custom_fields': {
                #     'full_name': user.get_full_name()
                # }
            })
            if result.is_success:
                customer = result.customer
                logger.info('Created new Braintree customer [%s].',
                            customer_id)
            else:
                # TODO Handle customer creation errors
                logger.error(result)
                pass

            if customer:
                params['customer_id'] = customer.id
        return braintree.ClientToken.generate(params)

    @classmethod
    def braintree_object_tree_to_dict(cls, braintree_object):
        data = braintree_object.__dict__.copy()
        for key, value in data.items():
            if isinstance(value, AttributeGetter):
                data[key] = cls.braintree_object_tree_to_dict(value)
        return data

    def create_transaction(self, nonce, basket):

        result = braintree.Transaction.sale({
            'amount': basket.total_incl_tax,
            'customer_id': basket.owner.get_username(),
            'options': {
                'submit_for_settlement': True,
                'store_in_vault_on_success': True,
            },
            'order_id': basket.order_number,
            'payment_method_nonce': nonce,
        })
        transaction = result.transaction
        if result.is_success:
            # Create Source to track all transactions related to this processor and order
            source_type, __ = SourceType.objects.get_or_create(name=self.NAME)
            currency = basket.currency
            total = basket.total_incl_tax
            transaction_id = transaction.id

            # TODO Check for PayPal data
            label = None
            card_type = None

            if transaction.credit_card_details.bin:
                label = transaction.credit_card_details.masked_number if transaction.credit_card['bin'] else None
                card_type = transaction.credit_card_details.card_type
            elif hasattr(transaction, 'paypal_details'):
                email = transaction.paypal_details.payer_email
                label = 'PayPal ({})'.format(email) if email else 'PayPal Account'
            elif hasattr(transaction, 'coinbase_details'):
                email = transaction.coinbase_details.user_email
                label = 'Coinbase ({})'.format(email) if email else 'Coinbase Account'

            # TODO Serialize transaction to JSON
            # self.record_processor_response(self.braintree_object_tree_to_dict(transaction),
            # transaction_id=transaction_id, basket=basket)

            source = Source(source_type=source_type,
                            currency=currency,
                            amount_allocated=total,
                            amount_debited=total,
                            reference=transaction_id,
                            label=label,
                            card_type=card_type)

            # Create PaymentEvent to track
            event_type, __ = PaymentEventType.objects.get_or_create(
                name=PaymentEventTypeName.PAID
            )
            event = PaymentEvent(event_type=event_type,
                                 amount=total,
                                 reference=transaction_id,
                                 processor_name=self.NAME)
            return source, event
        elif transaction:
            logger.error('Transaction failed: [%s] - [%s]',
                         transaction.processor_response_code,
                         transaction.processor_response_text)
            raise GatewayError

        else:
            # TODO Log the error!
            # for error in result.errors.deep_errors:
            #     print("attribute: " + error.attribute)
            #     print("  code: " + error.code)
            #     print("  message: " + error.message)
            raise GatewayError
