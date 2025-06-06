# Prediction interface for Cog ⚙️
# https://github.com/replicate/cog/blob/main/docs/python.md

from cog import BasePredictor, Input, Path
import os
import re
import time
import torch
import subprocess
import numpy as np
from PIL import Image
from typing import List
from diffusers import (
    FluxPipeline,
    FluxImg2ImgPipeline
)
from torchvision import transforms
from weights import WeightsDownloadCache
from transformers import CLIPImageProcessor
from lora_loading_patch import load_lora_into_transformer
from diffusers.pipelines.stable_diffusion.safety_checker import (
    StableDiffusionSafetyChecker
)
from sd_embed.embedding_funcs import get_weighted_text_embeddings_flux1


MAX_IMAGE_SIZE = 1440
MODEL_CACHE = "FLUX.1-schnell"
SAFETY_CACHE = "safety-cache"
FEATURE_EXTRACTOR = "/src/feature-extractor"
SAFETY_URL = "https://weights.replicate.delivery/default/sdxl/safety-1.0.tar"
MODEL_URL = "https://weights.replicate.delivery/default/black-forest-labs/FLUX.1-schnell/files.tar"

ASPECT_RATIOS = {
    "1:1": (1024, 1024),
    "16:9": (1344, 768),
    "21:9": (1536, 640),
    "3:2": (1216, 832),
    "2:3": (832, 1216),
    "4:5": (896, 1088),
    "5:4": (1088, 896),
    "3:4": (896, 1152),
    "4:3": (1152, 896),
    "9:16": (768, 1344),
    "9:21": (640, 1536),
}

def download_weights(url, dest, file=False):
    start = time.time()
    print("downloading url: ", url)
    print("downloading to: ", dest)
    if not file:
        subprocess.check_call(["pget", "-xf", url, dest], close_fds=False)
    else:
        subprocess.check_call(["pget", url, dest], close_fds=False)
    print("downloading took: ", time.time() - start)

class Predictor(BasePredictor):
    def setup(self) -> None:
        """Load the model into memory to make running multiple predictions efficient"""
        start = time.time()

        self.weights_cache = WeightsDownloadCache()
        self.last_loaded_loras = {}
        self.last_loaded_lora_scales = {}

        print("Loading safety checker...")
        if not os.path.exists(SAFETY_CACHE):
            download_weights(SAFETY_URL, SAFETY_CACHE)
        self.safety_checker = StableDiffusionSafetyChecker.from_pretrained(
            SAFETY_CACHE, torch_dtype=torch.float16
        ).to("cuda")
        self.feature_extractor = CLIPImageProcessor.from_pretrained(FEATURE_EXTRACTOR)
        
        print("Loading Flux txt2img Pipeline")
        if not os.path.exists(MODEL_CACHE):
            download_weights(MODEL_URL, '.')
        self.txt2img_pipe = FluxPipeline.from_pretrained(
            MODEL_CACHE,
            torch_dtype=torch.bfloat16,
            cache_dir=MODEL_CACHE
        ).to("cuda")
        self.txt2img_pipe.__class__.load_lora_into_transformer = classmethod(
            load_lora_into_transformer
        )

        print("Loading Flux img2img pipeline")
        self.img2img_pipe = FluxImg2ImgPipeline(
            transformer=self.txt2img_pipe.transformer,
            scheduler=self.txt2img_pipe.scheduler,
            vae=self.txt2img_pipe.vae,
            text_encoder=self.txt2img_pipe.text_encoder,
            text_encoder_2=self.txt2img_pipe.text_encoder_2,
            tokenizer=self.txt2img_pipe.tokenizer,
            tokenizer_2=self.txt2img_pipe.tokenizer_2,
        ).to("cuda")
        self.img2img_pipe.__class__.load_lora_into_transformer = classmethod(
            load_lora_into_transformer
        )
        
        print("setup took: ", time.time() - start)

    @torch.amp.autocast('cuda')
    def run_safety_checker(self, image):
        safety_checker_input = self.feature_extractor(image, return_tensors="pt").to("cuda")
        np_image = [np.array(val) for val in image]
        image, has_nsfw_concept = self.safety_checker(
            images=np_image,
            clip_input=safety_checker_input.pixel_values.to(torch.float16),
        )
        return image, has_nsfw_concept

    def aspect_ratio_to_width_height(self, aspect_ratio: str) -> tuple[int, int]:
        return ASPECT_RATIOS[aspect_ratio]

    def get_image(self, image: str):
        image = Image.open(image).convert("RGB")
        transform = transforms.Compose(
            [
                transforms.ToTensor(),
                transforms.Lambda(lambda x: 2.0 * x - 1.0),
            ]
        )
        img: torch.Tensor = transform(image)
        return img[None, ...]

    @staticmethod
    def make_multiple_of_16(n):
        return ((n + 15) // 16) * 16
    
    def load_loras(self, hf_loras, lora_scales):
        adapter_names = []
        # loop through each lora
        for idx, hf_lora in enumerate(hf_loras):
            t1 = time.time()
            adapter_name = f"adapter_{idx:03d}"
            adapter_names.append(adapter_name)

            # Check for Huggingface Slug lucataco/flux-emoji
            if re.match(r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+$", hf_lora):
                print(f"Downloading LoRA weights from - HF path: {hf_lora}")
                self.txt2img_pipe.load_lora_weights(hf_lora, adapter_name=adapter_name)
            # Check for Replicate tar file
            elif re.match(r"^https?://replicate.delivery/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+/trained_model.tar", hf_lora):
                print(f"Downloading LoRA weights from - Replicate URL: {hf_lora}")
                local_weights_cache = self.weights_cache.ensure(hf_lora)
                lora_path = os.path.join(local_weights_cache, "output/flux_train_replicate/lora.safetensors")
                self.txt2img_pipe.load_lora_weights(lora_path, adapter_name=adapter_name)
            # Check for Huggingface URL
            elif re.match(r"^https?://huggingface.co", hf_lora):
                print(f"Downloading LoRA weights from - HF URL: {hf_lora}")
                huggingface_slug = re.search(r"^https?://huggingface.co/([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)", hf_lora).group(1)
                weight_name = hf_lora.split('/')[-1]
                print(f"HuggingFace slug from URL: {huggingface_slug}, weight name: {weight_name}")
                self.txt2img_pipe.load_lora_weights(huggingface_slug, weight_name=weight_name)
            # Check for Civitai URL
            elif re.match(r"^https?://civitai.com/api/download/models/[0-9]+\?type=Model&format=SafeTensor", hf_lora):
                # split url to get first part of the url, everythin before '?type'
                civitai_slug = hf_lora.split('?type')[0]
                print(f"Downloading LoRA weights from - Civitai URL: {civitai_slug}")
                lora_path = self.weights_cache.ensure(hf_lora, file=True)
                self.txt2img_pipe.load_lora_weights(lora_path, adapter_name=adapter_name)
            # Check for URL to a .safetensors file
            elif hf_lora.endswith('.safetensors'):
                print(f"Downloading LoRA weights from - safetensor URL: {hf_lora}")
                try:
                    lora_path = self.weights_cache.ensure(hf_lora, file=True)
                except Exception as e:
                    print(f"Error downloading LoRA weights: {e}")
                    continue
                self.txt2img_pipe.load_lora_weights(lora_path, adapter_name=adapter_name)
            else:
                raise Exception(f"Invalid lora, must be either a: HuggingFace path, Replicate model.tar, or a URL to a .safetensors file: {hf_lora}")
            t2 = time.time()
            print(f"Loading LoRA took: {t2 - t1:.2f} seconds")
        
        # print(f"adapter_names: {adapter_names}")
        # print(f"adapter_weights: {adapter_weights}")
        self.last_loaded_loras = hf_loras
        self.last_loaded_lora_scales = lora_scales
        self.txt2img_pipe.set_adapters(adapter_names, adapter_weights=lora_scales)
            
    @torch.inference_mode()
    def predict(
        self,
        prompt: str = Input(description="Prompt for generated image"),
        aspect_ratio: str = Input(
            description="Aspect ratio for the generated image",
            choices=list(ASPECT_RATIOS.keys()),
            default="1:1"
        ),
        image: Path = Input(
            description="Input image for image to image mode. The aspect ratio of your output will match this image",
            default=None,
        ),
        prompt_strength: float = Input(
            description="Prompt strength (or denoising strength) when using image to image. 1.0 corresponds to full destruction of information in image.",
            ge=0,le=1,default=0.8,
        ),
        num_outputs: int = Input(
            description="Number of images to output.",
            ge=1,
            le=4,
            default=4,
        ),
        num_inference_steps: int = Input(
            description="Number of inference steps",
            ge=1,le=30,default=4,
        ),
        guidance_scale: float = Input(
            description="Guidance scale for the diffusion process",
            ge=0,le=15,default=3.5,
        ),
        seed: int = Input(description="Random seed. Set for reproducible generation", default=None),
        output_format: str = Input(
            description="Format of the output images",
            choices=["webp", "jpg", "png"],
            default="webp",
        ),
        output_quality: int = Input(
            description="Quality when saving the output images, from 0 to 100. 100 is best quality, 0 is lowest quality. Not relevant for .png outputs",
            default=80,
            ge=0,
            le=100,
        ),
        hf_loras: list[str] = Input(
            description="Huggingface path, or URL to the LoRA weights. Ex: alvdansen/frosting_lane_flux",
            default=None,
        ),
        lora_scales: list[float] = Input(
            description="Scale for the LoRA weights. Default value is 0.8",
            default=None,
        ),
        disable_safety_checker: bool = Input(
            description="Disable safety checker for generated images. This feature is only available through the API. See [https://replicate.com/docs/how-does-replicate-work#safety](https://replicate.com/docs/how-does-replicate-work#safety)",
            default=False,
        ),
    ) -> List[Path]:
        """Run a single prediction on the model"""
        if seed is None:
            seed = int.from_bytes(os.urandom(2), "big")
        print(f"Using seed: {seed}")

        width, height = self.aspect_ratio_to_width_height(aspect_ratio)
        max_sequence_length=512

        flux_kwargs = {"width": width, "height": height}
        print(f"Prompt: {prompt}")
        device = self.txt2img_pipe.device
        
        if image:
            pipe = self.img2img_pipe
            print("img2img mode")
            init_image = self.get_image(image)
            width = init_image.shape[-1]
            height = init_image.shape[-2]
            print(f"Input image size: {width}x{height}")
            # Calculate the scaling factor if the image exceeds MAX_IMAGE_SIZE
            scale = min(MAX_IMAGE_SIZE / width, MAX_IMAGE_SIZE / height, 1)
            if scale < 1:
                width = int(width * scale)
                height = int(height * scale)
                print(f"Scaling image down to {width}x{height}")

            # Round image width and height to nearest multiple of 16
            width = self.make_multiple_of_16(width)
            height = self.make_multiple_of_16(height)
            print(f"Input image size set to: {width}x{height}")
            # Resize
            init_image = init_image.to(device)
            init_image = torch.nn.functional.interpolate(init_image, (height, width))
            init_image = init_image.to(torch.bfloat16)
            # Set params
            flux_kwargs["image"] = init_image
            flux_kwargs["strength"] = prompt_strength
        else:
            print("txt2img mode")
            pipe = self.txt2img_pipe
        
        if hf_loras:
            if len(hf_loras) != len(lora_scales):
                raise Exception(f"number of lora scales ({len(lora_scales)}) does not match number of loras ({len(hf_loras)})")

            flux_kwargs["joint_attention_kwargs"] = {"scale": 1.0}
            # check if loras are new
            if hf_loras != self.last_loaded_loras or lora_scales != self.last_loaded_lora_scales:
                pipe.unload_lora_weights()
                self.load_loras(hf_loras, lora_scales)
        else:
            flux_kwargs["joint_attention_kwargs"] = None
            pipe.unload_lora_weights()

        # Ensure the pipeline is on GPU
        pipe = pipe.to("cuda")

        generator = torch.Generator("cuda").manual_seed(seed)

        (prompt_embeds, pooled_prompt_embeds) = get_weighted_text_embeddings_flux1(
          pipe,
          prompt = prompt
        )

        common_args = {
            "prompt_embeds": prompt_embeds,
            "pooled_prompt_embeds": pooled_prompt_embeds,
            "guidance_scale": guidance_scale,
            "generator": generator,
            "num_inference_steps": num_inference_steps,
            "max_sequence_length": max_sequence_length,
            "num_images_per_prompt": num_outputs,
            "output_type": "pil",
        }

        output = pipe(**common_args, **flux_kwargs)

        if not disable_safety_checker:
            _, has_nsfw_content = self.run_safety_checker(output.images)

        output_paths = []
        for i, image in enumerate(output.images):
            if not disable_safety_checker and has_nsfw_content[i]:
                print(f"NSFW content detected in image {i}")
                continue
            output_path = f"/tmp/out-{i}.{output_format}"
            if output_format != 'png':
                image.save(output_path, quality=output_quality, optimize=True)
            else:
                image.save(output_path)
            output_paths.append(Path(output_path))

        if len(output_paths) == 0:
            raise Exception("NSFW content detected. Try running it again, or try a different prompt.")

        return output_paths
    
