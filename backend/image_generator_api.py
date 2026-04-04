#!/usr/bin/env python3
"""
Simple Image Generation API for SillyTavern
Uses Hugging Face Diffusers for Stable Diffusion
Runs on port 5000 with REST API
Stores images in gallery for consistent references
"""

import os
import sys
# Force UTF-8 stdout/stderr on Windows to handle any unicode characters in logs
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from diffusers import StableDiffusionPipeline
import torch
import io
import base64
import time
# Look for image_gallery.py next to this script (Docker layout) and also
# in the user's home dir (native dev layout) for backward compatibility.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.expanduser('~'))
from image_gallery import ImageGallery

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Add manual CORS headers as backup
@app.before_request
def handle_preflight():
    if request.method == 'OPTIONS':
        response = app.make_default_options_response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 200

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

# Initialize the model (loads on first request)
pipe = None
device = "cuda" if torch.cuda.is_available() else "cpu"
gallery = ImageGallery()
model_load_start_time = None

def get_pipeline():
    global pipe, model_load_start_time
    if pipe is None:
        model_load_start_time = time.time()
        print("\n[MODEL] Starting to load Stable Diffusion pipeline...")
        print("[MODEL] This may take 30-60 seconds on first run...")
        sys.stdout.flush()

        pipe = StableDiffusionPipeline.from_pretrained(
            "runwayml/stable-diffusion-v1-5",
            torch_dtype=torch.float16 if device == "cuda" else torch.float32,
            safety_checker=None,  # Disable for speed
        )
        pipe = pipe.to(device)
        pipe.enable_attention_slicing()  # Reduce memory usage

        load_time = time.time() - model_load_start_time
        print(f"[MODEL] Model loaded successfully in {load_time:.1f} seconds!")
        sys.stdout.flush()
    return pipe

@app.route('/api/generate', methods=['POST', 'OPTIONS'])
def generate_image():
    """Generate an image from a text prompt"""
    # Handle preflight OPTIONS request
    if request.method == 'OPTIONS':
        from flask import Response
        resp = Response()
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS, GET'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, application/json'
        resp.headers['Access-Control-Max-Age'] = '3600'
        return resp, 200

    try:
        data = request.json
        prompt = data.get('prompt', 'a beautiful alien fortress')
        negative_prompt = data.get('negative_prompt', 'blurry, low quality, distorted')
        steps = int(data.get('steps', 25))
        guidance_scale = float(data.get('guidance_scale', 7.5))

        gen_start_time = time.time()
        print(f"\n{'='*60}")
        print(f"[API] Image generation request")
        print(f"[API] Prompt: {prompt[:80]}...")
        sys.stdout.flush()

        pipeline = get_pipeline()
        print(f"[API] Pipeline ready, starting generation with {steps} steps...")
        print(f"[API] Estimated generation time: 30-60 seconds...")
        sys.stdout.flush()

        # Callback to show progress during generation
        def progress_callback(step, timestep, latents):
            if step % 5 == 0 or step == steps - 1:  # Show every 5 steps
                elapsed = time.time() - gen_start_time
                print(f"[PROGRESS] Step {step+1}/{steps} - Elapsed: {elapsed:.1f}s")
                sys.stdout.flush()

        with torch.no_grad():
            image = pipeline(
                prompt=prompt,
                negative_prompt=negative_prompt,
                num_inference_steps=steps,
                guidance_scale=guidance_scale,
                height=512,
                width=512,
                callback=progress_callback,
                callback_steps=1,
            ).images[0]

        gen_time = time.time() - gen_start_time
        print(f"[API] Generation complete in {gen_time:.1f}s, encoding to base64...")
        sys.stdout.flush()

        # Convert to base64
        buffered = io.BytesIO()
        image.save(buffered, format="PNG")
        img_base64 = base64.b64encode(buffered.getvalue()).decode()

        # Store in gallery for consistent references
        image_id = gallery.add_image(
            image_data=img_base64,
            description=prompt,
            category="scenes"
        )

        print(f"[API] SUCCESS! Image ID: {image_id}")
        print(f"{'='*60}\n")
        sys.stdout.flush()

        response = jsonify({
            'success': True,
            'image': f'data:image/png;base64,{img_base64}',
            'prompt': prompt,
            'image_id': image_id
        })
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

    except Exception as e:
        print(f"\n[API] ERROR: {e}")
        import traceback
        traceback.print_exc()
        print(f"{'='*60}\n")
        sys.stdout.flush()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/gallery', methods=['GET'])
def get_gallery():
    """Get all stored images from the gallery"""
    images = gallery.list_all()
    return jsonify({'success': True, 'images': images})

@app.route('/api/gallery/<image_id>', methods=['GET'])
def get_gallery_image(image_id):
    """Retrieve a specific image from the gallery"""
    image_data = gallery.get_image_by_id(image_id)
    if image_data:
        return jsonify({
            'success': True,
            'image': f'data:image/png;base64,{image_data["image_data"]}',
            'description': image_data['description'],
            'id': image_id
        })
    return jsonify({'success': False, 'error': 'Image not found'}), 404

@app.route('/api/gallery/search', methods=['POST'])
def search_gallery():
    """Search for similar images in the gallery"""
    data = request.json
    description = data.get('description', '')
    match = gallery.find_similar_image(description)
    if match:
        image_data = gallery.get_image_by_id(match['id'])
        return jsonify({
            'success': True,
            'found': True,
            'image': f'data:image/png;base64,{image_data["image_data"]}',
            'match_description': match['description'],
            'id': match['id']
        })
    return jsonify({'success': True, 'found': False})

@app.route('/api/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({'status': 'running', 'device': device})

@app.route('/', methods=['GET'])
def index():
    """Root endpoint"""
    return '''
    <html>
    <head><title>Image Generator API</title></head>
    <body>
    <h1>Image Generator API Running</h1>
    <p>Device: {}</p>
    <p>API Endpoint: POST /api/generate</p>
    <pre>
    {{
        "prompt": "a beautiful alien fortress",
        "negative_prompt": "blurry, low quality",
        "steps": 25,
        "guidance_scale": 7.5
    }}
    </pre>
    </body>
    </html>
    '''.format(device)

if __name__ == '__main__':
    host = os.environ.get('FLASK_HOST', '127.0.0.1')
    port = int(os.environ.get('FLASK_PORT', '5000'))
    print(f"Starting Image Generator API")
    print(f"Device: {device}")
    print(f"Running on http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
