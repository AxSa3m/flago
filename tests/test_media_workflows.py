import pytest

from flgo.config import Settings
from flgo.media_workflows import (
    MediaWorkflowRegistry,
    MediaWorkflowRequest,
    MockMediaWorkflowProvider,
    build_media_workflow_registry,
)
from flgo.media_workflows.registry import media_workflow_provider_configs
from flgo.media_workflows.types import MediaJobStatus
from flgo.model_providers.types import ProviderCapability


@pytest.mark.asyncio
async def test_mock_media_workflow_provider_submits_video_job() -> None:
    provider = MockMediaWorkflowProvider(capabilities=[ProviderCapability.VIDEO_GENERATION])
    registry = MediaWorkflowRegistry()
    registry.register(provider)
    request = MediaWorkflowRequest(
        request_id="req-video",
        capability=ProviderCapability.VIDEO_GENERATION,
        prompt="生成一个产品演示视频",
    )

    result = await registry.submit("mock-media", request)

    assert result.status == MediaJobStatus.SUCCEEDED
    assert result.provider == "mock-media"
    assert result.assets[0].type == "video"
    assert provider.requests == [request]


def test_media_workflow_registry_rejects_missing_capability() -> None:
    registry = MediaWorkflowRegistry()
    registry.register(MockMediaWorkflowProvider(capabilities=[ProviderCapability.IMAGE_GENERATION]))

    with pytest.raises(ValueError) as exc_info:
        registry.resolve("mock-media", ProviderCapability.WORKFLOW_EXECUTION)

    assert "workflow_execution" in str(exc_info.value)


def test_media_workflow_configs_are_loaded_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        env="test",
        seedance_api_key="seedance-key",
        seedance_base_url="https://seedance.example",
        seedance_model="seedance-v1",
        comfyui_base_url="http://comfyui.local:8188",
        coze_api_key="coze-key",
        coze_base_url="https://coze.example",
        dify_api_key="dify-key",
        dify_base_url="https://dify.example",
    )

    configs = media_workflow_provider_configs(settings)

    assert [config.name for config in configs] == ["seedance", "comfyui", "coze", "dify"]
    assert configs[0].capabilities == [ProviderCapability.VIDEO_GENERATION]
    assert ProviderCapability.WORKFLOW_EXECUTION in configs[1].capabilities
    assert configs[2].capabilities == [ProviderCapability.WORKFLOW_EXECUTION]
    assert configs[3].capabilities == [ProviderCapability.WORKFLOW_EXECUTION]


def test_build_media_workflow_registry_registers_configured_providers() -> None:
    settings = Settings(
        _env_file=None,
        env="test",
        seedance_api_key="seedance-key",
        seedance_base_url="https://seedance.example",
        comfyui_base_url="http://comfyui.local:8188",
    )

    registry = build_media_workflow_registry(settings)

    assert registry.names() == ["comfyui", "seedance"]
    assert registry.resolve("seedance", ProviderCapability.VIDEO_GENERATION).name == "seedance"
    assert registry.resolve("comfyui", ProviderCapability.IMAGE_GENERATION).name == "comfyui"
