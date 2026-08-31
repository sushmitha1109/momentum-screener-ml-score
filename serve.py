#!/usr/bin/env python3
"""
Serve momentum_screener.html with environment variables injected
"""
import os
import http.server
import socketserver
from pathlib import Path

PORT = int(os.getenv('PORT', 8000))
HOST = os.getenv('HOST', '127.0.0.1')

# Configuration from environment
API_ENDPOINT = os.getenv('PYTHON_API_BASE', 'http://localhost:5000/api')
API_KEY = os.getenv('API_KEY', '')
USE_PYTHON_API = os.getenv('USE_PYTHON_API', 'true').lower() == 'true'

class ConfiguredHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ['/', '/momentum_screener.html']:
            # Read HTML file
            html_path = Path(__file__).parent / 'momentum_screener.html'
            with open(html_path, 'r') as f:
                html_content = f.read()
            
            # Inject configuration before the script tag
            config_script = f'''    <script>
        // Injected environment configuration at runtime
        window.ENV = {{
            USE_PYTHON_API: {str(USE_PYTHON_API).lower()},
            PYTHON_API_BASE: '{API_ENDPOINT}',
            API_KEY: '{API_KEY}'
        }};
    </script>'''
            
            # Replace the opening script tag with our config
            html_content = html_content.replace('<script>', config_script + '\n    <script>')
            
            # Send response
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.send_header('Content-Length', len(html_content.encode()))
            self.end_headers()
            self.wfile.write(html_content.encode())
            return
        
        # For other requests, use default handler
        super().do_GET()

# Print configuration
print(f'''
╔════════════════════════════════════════════╗
║   MOMENTUM SCREENER - HTML SERVER          ║
╚════════════════════════════════════════════╝

🔧 Configuration:
   USE_PYTHON_API: {USE_PYTHON_API}
   API_ENDPOINT: {API_ENDPOINT}
   API_KEY: {'***' + API_KEY[-8:] if API_KEY else 'Not set'}

🌐 Server:
   Host: {HOST}
   Port: {PORT}
   URL: http://{HOST}:{PORT}

⏱️  Starting...
''')

# Start server
with socketserver.TCPServer(
    (HOST, PORT),
    ConfiguredHTTPHandler
) as httpd:
    print(f'✅ Server running. Press Ctrl+C to stop.\n')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n\n🛑 Server stopped.')
