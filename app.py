import os
import json
import logging
import asyncio
import httpx
from datetime import datetime, timezone
from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, PlainTextResponse
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.exceptions import HTTPException
from pydantic import BaseModel, Field

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment variables for API keys and Lemon Squeezy settings
LEMON_SQUEEZY_API_KEY = os.getenv("LEMON_SQUEEZY_API_KEY")
LEMON_SQUEEZY_STORE_ID = os.getenv("LEMON_SQUEEZY_STORE_ID")
LEMON_SQUEEZY_PRODUCT_ID = os.getenv("LEMON_SQUEEZY_PRODUCT_ID")
LEMON_SQUEEZY_VARIANT_ID = os.getenv("LEMON_SQUEEZY_VARIANT_ID")
LEONARDO_AI_API_KEY = os.getenv("LEONARDO_AI_API_KEY")
SEGMIND_API_KEY = os.getenv("SEGMIND_API_KEY")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") # Shared secret for webhook validation

if not all([LEMON_SQUEEZY_API_KEY, LEMON_SQUEEZY_STORE_ID, LEMON_SQUEEZY_PRODUCT_ID, LEMON_SQUEEZY_VARIANT_ID, LEONARDO_AI_API_KEY, SEGMIND_API_KEY, WEBHOOK_SECRET]):
    logger.error("Missing one or more required environment variables.")
    # In a real deployment, you might want to exit or raise an exception here.
    # For now, we'll continue and handle errors at runtime.

# API Endpoints
LEMON_SQUEEZY_API_BASE = "https://api.lemonsqueezy.com/v1"
LEONARDO_AI_API_BASE = "https://cloud.leonardo.ai/api/v1"
SEGMIND_API_BASE = "https://api.segmind.com/v1"

# In-memory store for pending generations and successful payments
# This is a simplification; in production, use a database like Redis or PostgreSQL
pending_generations = {} # {session_id: {"prompt": prompt, "model": model, "status": "pending_payment", "created_at": datetime_obj}}
successful_payments = {} # {session_id: {"expires_at": datetime_obj}}

# Load models configuration from models.json
MODELS_CONFIG = {}
try:
    with open("models.json", "r") as f:
        MODELS_CONFIG = json.load(f)
    logger.info("models.json loaded successfully.")
except FileNotFoundError:
    logger.error("models.json not found. Ensure it's in the same directory as app.py.")
except json.JSONDecodeError:
    logger.error("Error decoding models.json. Check for syntax errors.")

def get_current_utc_time():
    """Returns the current UTC time as a timezone-aware datetime object."""
    return datetime.now(timezone.utc)

class GenerateRequest(BaseModel):
    model: str 
    prompt: str
    size: str = "1024x1024"
    n: int = 1
  
class WebhookRequest(BaseModel):
    meta: dict
    data: dict

async def homepage(request):
    return HTMLResponse("<h1>AI Image Router</h1><p>Send a POST request to /generate to create an image.</p><p>View <a href='/models-config'>/models-config</a> for available models.</p>")

async def models_config_page(request):
    return JSONResponse(MODELS_CONFIG)

async def generate_image(request):
    session_id = request.headers.get("X-Session-ID", os.urandom(16).hex())
    try:
        data = await request.json()
        req = GenerateRequest(**data)
        prompt = req.prompt
        model_choice = req.model
        width = req.width
        height = req.height

        # Check for active payment session
        if session_id in successful_payments and successful_payments[session_id]["expires_at"] > get_current_utc_time():
            logger.info(f"Session {session_id} has active payment. Proceeding with generation.")
            try:
                if model_choice == "leonardo":
                    image_url = await generate_with_leonardo(prompt, width, height)
                elif model_choice == "segmind":
                    image_url = await generate_with_segmind(prompt, width, height)
                else:
                    raise HTTPException(status_code=400, detail="Invalid model choice.")

                return JSONResponse({"image_url": image_url})

            except httpx.HTTPStatusError as e:
                logger.error(f"API Error for {model_choice}: {e.response.text}")
                raise HTTPException(status_code=500, detail=f"Image generation failed with {model_choice} API: {e.response.text}")
            except Exception as e:
                logger.error(f"Unexpected error during image generation: {e}")
                raise HTTPException(status_code=500, detail=f"An unexpected error occurred during image generation: {str(e)}")
        else:
            # Payment required, create checkout URL
            checkout_url = await create_lemon_squeezy_checkout(session_id)
            pending_generations[session_id] = {
                "prompt": prompt,
                "model": model_choice,
                "width": width,
                "height": height,
                "status": "pending_payment",
                "created_at": get_current_utc_time()
            }
            logger.info(f"Payment required for session {session_id}. Checkout URL generated.")
            return JSONResponse({"checkout_url": checkout_url, "session_id": session_id, "message": "Payment required."}, status_code=402)

    except HTTPException:
        raise # Re-raise Starlette HTTPExceptions
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON format.")
    except Exception as e:
        logger.error(f"Error in generate_image endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")

async def lemon_squeezy_webhook(request):
    try:
        # Validate webhook signature (important for security)
        signature = request.headers.get("X-Signature")
        if not signature:
            raise HTTPException(status_code=401, detail="Missing X-Signature header.")

        # In a real application, you would verify the signature using WEBHOOK_SECRET
        # For simplicity, we'll skip the actual signature verification here,
        # but it's CRUCIAL for production.
        # Example verification (using hmac and hashlib):
        # hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest() == signature

        body = await request.json()
        event_type = body.get("meta", {}).get("event_name")
        custom_data = body.get("meta", {}).get("custom_data", {})
        session_id = custom_data.get("session_id")

        if not session_id:
            logger.warning("Webhook received without session_id in custom_data.")
            return PlainTextResponse("No session_id in custom_data, ignoring.", status_code=200)

        if event_type == "order_created" or event_type == "subscription_created":
            # Assuming a one-time purchase or a simple subscription for generation credits
            # For a subscription, you'd manage access periods more robustly
            # For this example, we grant access for a fixed duration (e.g., 1 hour for testing)

            expires_at = get_current_utc_time() + timedelta(hours=1) # Grant 1 hour access for now
            successful_payments[session_id] = {"expires_at": expires_at}
            logger.info(f"Payment confirmed for session {session_id}. Access granted until {expires_at}.")

            # If there's a pending generation, attempt to fulfill it now
            if session_id in pending_generations:
                pending_gen = pending_generations[session_id]
                prompt = pending_gen["prompt"]
                model_choice = pending_gen["model"]
                width = pending_gen["width"]
                height = pending_gen["height"]
                logger.info(f"Attempting to fulfill pending generation for session {session_id}.")

                try:
                    if model_choice == "leonardo":
                        image_url = await generate_with_leonardo(prompt, width, height)
                    elif model_choice == "segmind":
                        image_url = await generate_with_segmind(prompt, width, height)
                    else:
                        raise ValueError("Invalid model choice in pending generation.")

                    # Store result or send to another system for delivery to user
                    logger.info(f"Pending generation for {session_id} fulfilled. Image URL: {image_url}")
                    # In a real app, you'd send this image_url back to the user via a persistent connection
                    # or update a database entry that the user can query.
                    # For this example, we just log it.
                    del pending_generations[session_id] # Clear pending after fulfillment

                except httpx.HTTPStatusError as e:
                    logger.error(f"API Error fulfilling pending generation for {session_id}: {e.response.text}")
                    # Handle error: notify user, retry, etc.
                except Exception as e:
                    logger.error(f"Unexpected error fulfilling pending generation for {session_id}: {e}")
                    # Handle error

            return PlainTextResponse("Webhook processed successfully.", status_code=200)

        elif event_type in ["order_refunded", "subscription_cancelled"]:
            # Handle refunds or cancellations (e.g., revoke access)
            if session_id in successful_payments:
                del successful_payments[session_id]
                logger.info(f"Access revoked for session {session_id} due to {event_type}.")
            return PlainTextResponse("Webhook processed (refund/cancellation).", status_code=200)

        else:
            logger.info(f"Unhandled webhook event type: {event_type}")
            return PlainTextResponse("Unhandled event type.", status_code=200)

    except HTTPException:
        raise # Re-raise Starlette HTTPExceptions
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return PlainTextResponse(f"Internal Server Error: {str(e)}", status_code=500)

async def create_lemon_squeezy_checkout(session_id: str):
    headers = {
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
        "Authorization": f"Bearer {LEMON_SQUEEZY_API_KEY}"
    }
    data = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "variant_id": int(LEMON_SQUEEZY_VARIANT_ID),
                "product_id": int(LEMON_SQUEEZY_PRODUCT_ID),
                "custom_data": {"session_id": session_id}
            }
        }
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{LEMON_SQUEEZY_API_BASE}/checkouts", headers=headers, json=data)
            response.raise_for_status()
            checkout_data = response.json()
            return checkout_data["data"]["attributes"]["url"]
    except httpx.HTTPStatusError as e:
        logger.error(f"Lemon Squeezy API Error creating checkout: {e.response.text}")
        raise HTTPException(status_code=500, detail=f"Failed to create checkout URL: {e.response.text}")
    except Exception as e:
        logger.error(f"Unexpected error creating Lemon Squeezy checkout: {e}")
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred creating checkout: {str(e)}")

async def generate_with_leonardo(prompt: str, width: int, height: int):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LEONARDO_AI_API_KEY}"
    }
    data = {
        "prompt": prompt,
        "modelId": "6bef9f1b-29cb-40c7-b9c3-37d6cecc23c4", # Stable Diffusion XL
        "width": width,
        "height": height,
        "num_images": 1,
        "sd_version": "v1_0", # SDXL 1.0 (ensure it matches the modelId compatibility)
        "alchemy": False,
        "presetStyle": "CINEMATIC",
        "public": False # Keep generations private
    }

    try:
        async with httpx.AsyncClient() as client:
            # First, request generation
            response = await client.post(f"{LEONARDO_AI_API_BASE}/generations", headers=headers, json=data)
            response.raise_for_status()
            generation_id = response.json()["sdGenerationJob"]["generationId"]
            logger.info(f"Leonardo AI generation job started with ID: {generation_id}")

            # Poll for completion
            for _ in range(30): # Poll up to 30 times (e.g., 30 * 2 seconds = 1 minute)
                await asyncio.sleep(2)
                status_response = await client.get(f"{LEONARDO_AI_API_BASE}/generations/{generation_id}", headers=headers)
                status_response.raise_for_status()
                status_data = status_response.json()

                if status_data["sdGenerationJob"]["status"] == "COMPLETE":
                    image_url = status_data["sdGenerationJob"]["generated_images"][0]["url"]
                    logger.info(f"Leonardo AI generation complete. Image URL: {image_url}")
                    return image_url
                elif status_data["sdGenerationJob"]["status"] == "FAILED":
                    raise HTTPException(status_code=500, detail="Leonardo AI generation failed.")

            raise HTTPException(status_code=504, detail="Leonardo AI generation timed out.")

    except httpx.HTTPStatusError as e:
        logger.error(f"Leonardo AI API Error: {e.response.text}")
        raise HTTPException(status_code=500, detail=f"Leonardo AI generation failed: {e.response.text}")
    except Exception as e:
        logger.error(f"Unexpected error with Leonardo AI generation: {e}")
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred with Leonardo AI: {str(e)}")

async def generate_with_segmind(prompt: str, width: int, height: int):
    headers = {
        "X-Api-Key": SEGMIND_API_KEY,
        "Content-Type": "application/json"
    }
    data = {
        "prompt": prompt,
        "negative_prompt": "bad anatomy, blurry, distorted",
        "samples": 1,
        "scheduler": "dpm_solver",
        "num_inference_steps": 25,
        "guidance_scale": 7.5,
        "seed": -1,
        "img_width": width,
        "img_height": height,
        "base64": False
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{SEGMIND_API_BASE}/sdxl1.0-txt2img", headers=headers, json=data)
            response.raise_for_status()
            result = response.json()
            if "url" in result:
                logger.info(f"Segmind generation complete. Image URL: {result['url']}")
                return result["url"]
            elif "error" in result:
                logger.error(f"Segmind API Error: {result['error']}")
                raise HTTPException(status_code=500, detail=f"Segmind generation failed: {result['error']}")
            else:
                raise HTTPException(status_code=500, detail="Segmind API returned an unexpected response.")

    except httpx.HTTPStatusError as e:
        logger.error(f"Segmind API Error: {e.response.text}")
        raise HTTPException(status_code=500, detail=f"Segmind generation failed: {e.response.text}")
    except Exception as e:
        logger.error(f"Unexpected error with Segmind generation: {e}")
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred with Segmind: {str(e)}")

# Routes definition
routes = [
    Route("/", homepage),
    Route("/models-config", models_config_page),
    Route("/generate", generate_image, methods=["POST"]),
    Route("/webhook/lemonsqueezy", lemon_squeezy_webhook, methods=["POST"])
]

# Middleware for security and other functions
middleware = [
    Middleware(TrustedHostMiddleware, allowed_hosts=["*"]), # Adjust allowed_hosts in production
]

app = Starlette(routes=routes, middleware=middleware, debug=True)

# Add timedelta dynamically for compatibility
from datetime import timedelta
