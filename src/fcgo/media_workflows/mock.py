from fcgo.media_workflows.types import (
    MediaAsset,
    MediaJobStatus,
    MediaWorkflowProviderConfig,
    MediaWorkflowRequest,
    MediaWorkflowResult,
)
from fcgo.model_providers.types import ProviderCapability


class MockMediaWorkflowProvider:
    def __init__(
        self,
        name: str = "mock-media",
        capabilities: list[ProviderCapability] | None = None,
    ) -> None:
        self.name = name
        self.provider_config = MediaWorkflowProviderConfig(
            name=name,
            capabilities=capabilities
            or [
                ProviderCapability.IMAGE_GENERATION,
                ProviderCapability.VIDEO_GENERATION,
                ProviderCapability.WORKFLOW_EXECUTION,
            ],
        )
        self.requests: list[MediaWorkflowRequest] = []

    async def submit(self, request: MediaWorkflowRequest) -> MediaWorkflowResult:
        self.requests.append(request)
        asset_type = (
            "video"
            if request.capability == ProviderCapability.VIDEO_GENERATION
            else "image"
        )
        suffix = "mp4" if asset_type == "video" else "png"
        return MediaWorkflowResult(
            request_id=request.request_id,
            provider=self.name,
            status=MediaJobStatus.SUCCEEDED,
            job_id=f"mock-{request.request_id}",
            assets=[
                MediaAsset(
                    type=asset_type,
                    media_type="video/mp4" if asset_type == "video" else "image/png",
                    url=f"mock://{self.name}/{request.request_id}",
                    filename=f"{request.request_id}.{suffix}",
                )
            ],
            text="mock media workflow completed",
        )

    async def poll(self, request_id: str, job_id: str) -> MediaWorkflowResult:
        return MediaWorkflowResult(
            request_id=request_id,
            provider=self.name,
            status=MediaJobStatus.SUCCEEDED,
            job_id=job_id,
            text="mock media workflow completed",
        )
