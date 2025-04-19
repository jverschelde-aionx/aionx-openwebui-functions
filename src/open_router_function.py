"""
OpenRouter API integration for Open WebUI.

This module provides functionality to interact with the OpenRouter API,
allowing access to various AI models through a unified interface.
"""

import json
import time
from typing import Dict, List, Any, AsyncGenerator, Union

import httpx
import requests
from pydantic import BaseModel, Field


class Pipe:
    """
    Main class for handling OpenRouter API interactions.

    This class provides methods to list available models and to process
    chat completion requests with OpenRouter's API.
    """

    class Valves(BaseModel):
        """Configuration parameters for the OpenRouter API connection."""

        OPENROUTER_API_BASE_URL: str = Field(default="https://openrouter.ai/api/v1")
        OPENROUTER_API_KEY: str = Field(
            default="",
            description="Your OpenRouter API key for authentication",
        )
        FREE_ONLY: bool = Field(default=False)
        DEBUG: bool = Field(default=False)
        REQUEST_TIMEOUT: int = Field(default=300)

    def _get_headers(self) -> Dict[str, str]:
        """
        Generate the headers required for OpenRouter API requests.

        Returns:
            Dict[str, str]: Headers dictionary with authentication and metadata.
        """
        return {
            "Authorization": f"Bearer {self.valves.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://chat.aionx.be/",
            "X-Title": "Aionx Open WebUI",
        }

    def _debug(self, message: str) -> None:
        """
        Print debug messages if DEBUG is enabled.

        Args:
            message: The debug message to print.
        """
        if self.valves.DEBUG:
            print(message)

    def __init__(self) -> None:
        """Initialize the Pipe with default valve settings."""
        self.valves = self.Valves()

    def _handle_response(self, response: requests.Response) -> Dict[str, Any]:
        """
        Handle HTTP responses and extract JSON content.

        Args:
            response: The HTTP response object.

        Returns:
            Dict[str, Any]: Parsed JSON response.

        Raises:
            requests.exceptions.HTTPError: If the HTTP request fails.
            ValueError: If the response contains invalid JSON.
        """
        try:
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as exc:
            self._debug(f"HTTPError: {exc.response.text}")
            raise
        except ValueError as exc:
            self._debug(f"Invalid JSON response: {response.text}")
            raise

    def pipes(self) -> List[Dict[str, str]]:
        """
        List available models from OpenRouter.

        Returns:
            List[Dict[str, str]]: List of models with their IDs and names.
        """
        url = f"{self.valves.OPENROUTER_API_BASE_URL}/models"
        try:
            self._debug(f"Fetching models from {url}")
            response = requests.get(url, headers=self._get_headers(), timeout=30)
            models_data = self._handle_response(response).get("data", [])
            self._debug(f"Retrieved {len(models_data)} models")
            if self.valves.FREE_ONLY:
                models_data = [
                    model
                    for model in models_data
                    if "free" in model.get("id", "").lower()
                ]

            return [
                {
                    "id": model.get("id", "unknown"),
                    "name": model.get("name", "Unknown Model"),
                }
                for model in models_data
            ]
        except Exception as exc:
            self._debug(f"Failed to fetch models: {exc}")
            return [{"id": "openrouter", "name": str(exc)}]

    async def pipe(
        self, body: Dict[str, Any]
    ) -> Union[Dict[str, Any], str, AsyncGenerator[str, None]]:
        """
        Process a chat completion request to OpenRouter.

        Args:
            body: Request parameters including model, messages, etc.

        Returns:
            Union[Dict[str, Any], str, AsyncGenerator[str, None]]:
                Response data or a stream of response chunks.
        """
        modified_body = {**body}
        if "model" in modified_body:
            # Remove "reasoning/" prefix from model ID
            modified_body["model"] = (
                modified_body["model"].split(".", 1)[-1].replace("reasoning/", "", 1)
            )

        modified_body["include_reasoning"] = True

        try:
            if body.get("stream", False):
                return self._handle_streaming_request(modified_body)
            return self._handle_normal_request(modified_body)
        except Exception as exc:
            error_msg = f"Error processing request: {str(exc)}"
            self._debug(error_msg)
            return json.dumps({"error": error_msg})

    async def _handle_normal_request(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle a standard (non-streaming) chat completion request.

        Args:
            body: The request parameters.

        Returns:
            Dict[str, Any]: The completion response.

        Raises:
            httpx.HTTPStatusError: If the HTTP request fails.
        """
        async with httpx.AsyncClient(timeout=self.valves.REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{self.valves.OPENROUTER_API_BASE_URL}/chat/completions",
                json=body,
                headers=self._get_headers(),
            )
            response.raise_for_status()
            data = response.json()

            if "choices" in data:
                for choice in data["choices"]:
                    if "message" in choice and "reasoning" in choice["message"]:
                        reasoning = choice["message"]["reasoning"]
                        choice["message"][
                            "content"
                        ] = f"<think>{reasoning}</think>\n{choice['message']['content']}"
            return data

    async def _handle_streaming_request(
        self, body: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        """
        Handle a streaming chat completion request.

        Args:
            body: The request parameters.

        Yields:
            str: Chunks of the streamed response.

        Raises:
            httpx.HTTPStatusError: If the HTTP request fails.
        """

        def construct_chunk(content: str) -> str:
            """
            Construct a properly formatted SSE chunk.

            Args:
                content: The content to include in the chunk.

            Returns:
                str: Formatted SSE chunk.
            """
            chunk_data = {
                "id": data.get("id", ""),
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", "unknown"),
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": content, "role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            }
            return f"data: {json.dumps(chunk_data)}\n\n"

        async with httpx.AsyncClient(timeout=self.valves.REQUEST_TIMEOUT) as client:
            async with client.stream(
                "POST",
                f"{self.valves.OPENROUTER_API_BASE_URL}/chat/completions",
                json=body,
                headers=self._get_headers(),
            ) as response:
                # Fail fast if the response has an error status code
                response.raise_for_status()

                # State tracking for thinking/reasoning responses
                # -1: not started, 0: thinking, 1: answered
                thinking_state = -1
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue

                    data = json.loads(line[6:])
                    choice = data.get("choices", [{}])[0]
                    delta = choice.get("delta", {})

                    # State transitions
                    if thinking_state == -1 and delta.get("reasoning"):
                        thinking_state = 0
                        yield construct_chunk("<think>")

                    elif (
                        thinking_state == 0
                        and not delta.get("reasoning")
                        and delta.get("content")
                    ):
                        thinking_state = 1
                        yield construct_chunk("</think>\n\n")

                    # Handle content output
                    content = delta.get("reasoning") or delta.get("content", "")
                    if content:
                        yield construct_chunk(content)

                    if choice.get("finish_reason"):
                        yield "data: [DONE]\n\n"
                        return
