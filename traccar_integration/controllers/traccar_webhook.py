from odoo import http
from odoo.http import request, Response
import json
import logging

_logger = logging.getLogger(__name__)


class TraccarWebhookController(http.Controller):

    # SIMPLE TEST ENDPOINT - Debug first
    @http.route('/traccar/test', type='http', auth='public', methods=['GET'], csrf=False)
    def test_endpoint(self, **kwargs):
        _logger.info("=== TEST ENDPOINT HIT ===")
        return Response("Test successful! Webhook is working.", status=200)

    # MAIN WEBHOOK ENDPOINT - Simplified
    @http.route('/traccar/webhook', type='http', auth='public', methods=['POST', 'GET'], csrf=False)
    def traccar_webhook(self, **kwargs):
        _logger.info("=== WEBHOOK ENDPOINT CALLED ===")

        if request.httprequest.method == 'GET':
            _logger.info("GET request received - likely health check")
            return Response(json.dumps({'status': 'ready', 'message': 'Webhook endpoint active'}),
                            mimetype='application/json', status=200)

        try:
            # Get raw data
            raw_data = request.httprequest.get_data(as_text=True)
            _logger.info("Raw data received: %s", raw_data[:500])  # Log first 500 chars

            if not raw_data:
                _logger.warning("Empty payload received")
                return Response(json.dumps({'error': 'Empty payload'}),
                                mimetype='application/json', status=400)

            # Try to parse JSON
            try:
                data = json.loads(raw_data)
                _logger.info("JSON parsed successfully")
            except json.JSONDecodeError as e:
                _logger.error("JSON decode error: %s", str(e))
                return Response(json.dumps({'error': 'Invalid JSON'}),
                                mimetype='application/json', status=400)

            # Process the data
            result = self._process_traccar_data(data)

            return Response(json.dumps({'success': True, 'result': result}),
                            mimetype='application/json', status=200)

        except Exception as e:
            _logger.error("Unexpected error: %s", str(e), exc_info=True)
            return Response(json.dumps({'error': str(e)}),
                            mimetype='application/json', status=500)

    def _process_traccar_data(self, data):
        """Process Traccar webhook data"""
        _logger.info("Processing data: %s", json.dumps(data)[:200])
        return {"processed": True, "message": "Data received successfully"}