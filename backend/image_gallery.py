#!/usr/bin/env python3
"""
Image Gallery System for The Remnant Fortress Game
Stores generated images with descriptions for consistent references
"""

import json
import os
from datetime import datetime
from pathlib import Path
import hashlib

class ImageGallery:
    def __init__(self, gallery_dir="~/SillyTavern/data/default-user/images"):
        self.gallery_dir = Path(gallery_dir).expanduser()
        self.gallery_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.gallery_dir / "index.json"
        self.load_index()

    def load_index(self):
        """Load the image index"""
        if self.index_file.exists():
            with open(self.index_file, 'r', encoding='utf-8') as f:
                self.index = json.load(f)
        else:
            self.index = {
                'images': {},
                'locations': {},
                'npcs': {},
                'scenes': {}
            }

    def save_index(self):
        """Save the image index"""
        with open(self.index_file, 'w', encoding='utf-8') as f:
            json.dump(self.index, f, indent=2, ensure_ascii=False)

    def add_image(self, image_data, description, category="scenes", subcategory=None):
        """
        Store an image with metadata

        Args:
            image_data: base64 image data
            description: detailed description of the image
            category: "locations", "npcs", "scenes", etc.
            subcategory: optional subcategory (e.g., "fortress_interior")

        Returns:
            image_id: unique identifier for the image
        """
        # Generate unique ID from description hash
        image_hash = hashlib.md5(description.encode()).hexdigest()[:8]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        image_id = f"{category}_{timestamp}_{image_hash}"

        # Save image file
        image_path = self.gallery_dir / f"{image_id}.json"
        image_record = {
            'id': image_id,
            'created': datetime.now().isoformat(),
            'description': description,
            'category': category,
            'subcategory': subcategory,
            'image_data': image_data  # Base64 encoded
        }

        with open(image_path, 'w', encoding='utf-8') as f:
            json.dump(image_record, f, indent=2, ensure_ascii=False)

        # Update index
        if category not in self.index:
            self.index[category] = {}

        self.index[category][image_id] = {
            'description': description,
            'subcategory': subcategory,
            'created': image_record['created'],
            'path': str(image_path)
        }

        self.save_index()
        return image_id

    def get_image_by_id(self, image_id):
        """Retrieve an image by ID"""
        for category, images in self.index.items():
            if image_id in images:
                image_path = Path(images[image_id]['path'])
                if image_path.exists():
                    with open(image_path, 'r', encoding='utf-8') as f:
                        return json.load(f)
        return None

    def get_images_by_category(self, category):
        """Get all images in a category"""
        images = []
        if category in self.index:
            for image_id, metadata in self.index[category].items():
                image_data = self.get_image_by_id(image_id)
                if image_data:
                    images.append(image_data)
        return images

    def get_images_by_subcategory(self, category, subcategory):
        """Get images by category and subcategory"""
        images = []
        if category in self.index:
            for image_id, metadata in self.index[category].items():
                if metadata.get('subcategory') == subcategory:
                    image_data = self.get_image_by_id(image_id)
                    if image_data:
                        images.append(image_data)
        return images

    def find_similar_image(self, description):
        """Find a previously generated image with similar description"""
        # Simple keyword matching for now
        keywords = description.lower().split()
        matches = []

        for category, images in self.index.items():
            for image_id, metadata in images.items():
                image_desc = metadata['description'].lower()
                matching_keywords = sum(1 for kw in keywords if kw in image_desc)
                if matching_keywords >= 3:  # At least 3 keywords match
                    matches.append({
                        'id': image_id,
                        'description': metadata['description'],
                        'score': matching_keywords
                    })

        # Return best match
        if matches:
            return sorted(matches, key=lambda x: x['score'], reverse=True)[0]
        return None

    def list_all(self):
        """List all stored images with descriptions"""
        result = []
        for category, images in self.index.items():
            for image_id, metadata in images.items():
                result.append({
                    'id': image_id,
                    'category': category,
                    'description': metadata['description'],
                    'created': metadata['created']
                })
        return result


# Example usage:
if __name__ == "__main__":
    gallery = ImageGallery()
    print("Image Gallery initialized!")
    print(f"Gallery location: {gallery.gallery_dir}")
    print(f"Index file: {gallery.index_file}")
    print(f"Current images: {len([i for cat in gallery.index.values() for i in cat])}")
