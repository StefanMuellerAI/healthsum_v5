# extractors.py
from dotenv import load_dotenv
from abc import ABC, abstractmethod
from PyPDF2 import PdfReader
import pytesseract
import pdf2image
import base64
import io
import json
from azure.ai.vision.imageanalysis import ImageAnalysisClient
from azure.ai.vision.imageanalysis.models import VisualFeatures
from azure.core.credentials import AzureKeyCredential
from openai import OpenAI
import google.generativeai as genai
import os
import xml.etree.ElementTree as ET
import re
from flask_sqlalchemy import SQLAlchemy


load_dotenv()

# Initialisierung der Clients
vision_azure_client = ImageAnalysisClient(
    credential=AzureKeyCredential(os.getenv("AZURE_KEY_CREDENTIALS")),
    endpoint="https://benda-vision.cognitiveservices.azure.com/",
)

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
openai_model = os.environ["OPENAI_MODEL"]
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
gemini_model = genai.GenerativeModel("gemini-1.5-pro")

db = SQLAlchemy()


class Extractor(ABC):
    @abstractmethod
    def extract(self, file_path):
        pass

    def create_structured_output(self, method, file_name, page_texts):
        root = ET.Element("extraction", method=method)
        doc = ET.SubElement(root, "document", title=file_name)
        for i, text in enumerate(page_texts):
            page = ET.SubElement(doc, "page", number=str(i))
            page.text = text
        return ET.tostring(root, encoding="unicode")
    

class PDFTextExtractor(Extractor):
    def extract(self, file_path):
        page_texts = []
        with open(file_path, 'rb') as file:
            pdf_reader = PdfReader(file)
            for page in pdf_reader.pages:
                page_texts.append(page.extract_text())
        return self.create_structured_output("pdf_text", os.path.basename(file_path), page_texts)


class OCRExtractor(Extractor):
    def extract(self, file_path):
        page_texts = []
        images = pdf2image.convert_from_path(file_path)
        for image in images:
            page_text = pytesseract.image_to_string(image, lang='deu')
            page_texts.append(page_text)
        return self.create_structured_output("ocr", os.path.basename(file_path), page_texts)


class AzureVisionExtractor(Extractor):
    def extract(self, file_path):
        page_texts = []
        with open(file_path, 'rb') as file:
            pdf_bytes = file.read()

        seiten = pdf2image.convert_from_bytes(pdf_bytes)
        for seite in seiten:
            image_stream = self.seite_zu_image_stream(seite)
            result = vision_azure_client.analyze(
                image_data=image_stream,
                visual_features=[VisualFeatures.READ]
            )
            if result.read is not None:
                page_text = ' '.join(
                    [' '.join([word.text for word in line.words]) for block in result.read.blocks for line in
                     block.lines])
                page_texts.append(page_text)

        return self.create_structured_output("azure_vision", os.path.basename(file_path), page_texts)

    def seite_zu_image_stream(self, seite):
        img_byte_arr = io.BytesIO()
        seite.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0)
        return img_byte_arr


class GPT4VisionExtractor(Extractor):
    def extract(self, file_path):
        page_texts = []
        with open(file_path, 'rb') as file:
            pdf_bytes = file.read()

        seiten = pdf2image.convert_from_bytes(pdf_bytes)
        for seite in seiten:
            base64_image = self.seite_zu_base64(seite)
            response = openai_client.chat.completions.create(
                model=openai_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Wandele bitte das Bild in ein Json-Format um."},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}",
                                },
                            },
                        ],
                    }
                ],
                max_tokens=16000,
            )
            page_texts.append(response.choices[0].message.content)
        return self.create_structured_output("gpt4_vision", os.path.basename(file_path), page_texts)

    def seite_zu_base64(self, seite, max_size_kb=19000):
        img_bytes = io.BytesIO()
        seite.save(img_bytes, format='JPEG')
        base64_image = base64.b64encode(img_bytes.getvalue()).decode('utf-8')

        image_size_kb = len(base64_image) * 3 / 4 / 1024
        if image_size_kb > max_size_kb:
            buffer = io.BytesIO()
            seite.thumbnail((seite.width // 2, seite.height // 2))
            seite.save(buffer, format='JPEG', quality=85)
            buffer.seek(0)
            base64_image = base64.b64encode(buffer.read()).decode('utf-8')

            image_size_kb = len(base64_image) * 3 / 4 / 1024
            if image_size_kb > max_size_kb:
                buffer = io.BytesIO()
                seite.save(buffer, format='JPEG', quality=70)
                buffer.seek(0)
                base64_image = base64.b64encode(buffer.read()).decode('utf-8')

        return base64_image


class CodeExtractor(Extractor):
    def __init__(self):
        self.pattern = r"\b([A-Z]\d{1,2}(\.\d+)?|[A-Z]{2}\d{2}(\.\d+)?|\d-\d{3}(\.\d+)?|\d-\d{3}[a-z]?)\b"
    
    def extract(self, text):
        if not isinstance(text, str):
            raise ValueError("Input must be a string")
            
        # Codes extrahieren
        codes = re.findall(self.pattern, text)
        
        # Nur die Hauptgruppe der Matches extrahieren
        extracted_codes = [match[0] for match in codes]
        
        # Entferne Duplikate durch Umwandlung in ein Set und zurück in eine Liste
        extracted_codes = list(set(extracted_codes))
        
        # Gruppiere die Codes nach ihrem Typ
        icd10_codes = []
        icd11_codes = []
        ops_codes = []
        
        for code in extracted_codes:
            if re.match(r"[A-Z]\d{2}", code):  # ICD-10 Format
                icd10_codes.append(code)
            elif re.match(r"[A-Z]{2}\d{2}", code):  # ICD-11 Format
                icd11_codes.append(code)
            elif re.match(r"\d-\d{3}", code):  # OPS Format
                ops_codes.append(code)
        
        # Entferne Duplikate in den einzelnen Code-Listen (für zusätzliche Sicherheit)
        icd10_codes = list(set(icd10_codes))
        icd11_codes = list(set(icd11_codes))
        ops_codes = list(set(ops_codes))
        
        # Erstelle strukturierte XML-Ausgabe
        root = ET.Element("extraction", method="code_extraction")
        
        if icd10_codes:
            icd10_elem = ET.SubElement(root, "icd10_codes")
            for code in icd10_codes:
                code_elem = ET.SubElement(icd10_elem, "code")
                code_elem.text = code
                
        if icd11_codes:
            icd11_elem = ET.SubElement(root, "icd11_codes")
            for code in icd11_codes:
                code_elem = ET.SubElement(icd11_elem, "code")
                code_elem.text = code
                
        if ops_codes:
            ops_elem = ET.SubElement(root, "ops_codes")
            for code in ops_codes:
                code_elem = ET.SubElement(ops_elem, "code")
                code_elem.text = code
        
        return ET.tostring(root, encoding="unicode")