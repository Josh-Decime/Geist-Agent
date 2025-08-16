import re
from datetime import datetime
from typing import Optional


class ReportUtils:
    """Utility class for report generation functions"""
    
    @staticmethod
    def generate_filename(topic: Optional[str] = None, max_topic_length: int = 25) -> str:
        """
        Generate a filename based on topic and timestamp
        
        Args:
            topic: The topic/subject for the report
            max_topic_length: Maximum characters for topic (default 25)
            
        Returns:
            str: Formatted filename like "Topic_Name_08-15-2025_18-32.md"
        """
        try:
            # Handle missing or invalid topic
            if not topic or not isinstance(topic, str):
                safe_topic = "unknown_topic"
            else:
                # Clean topic for filename (remove special chars, limit length)
                safe_topic = re.sub(r'[^\w\s-]', '', topic)
                safe_topic = re.sub(r'[-\s]+', '_', safe_topic)
                safe_topic = safe_topic.strip('_')[:max_topic_length]
                
                # Fallback if topic becomes empty after cleaning
                if not safe_topic:
                    safe_topic = "unknown_topic"
            
            # Generate timestamp - fallback if datetime fails
            try:
                timestamp = datetime.now().strftime("%m/%d/%Y_%H:%M")
                # Replace slashes and colons for Windows filename compatibility
                safe_timestamp = timestamp.replace('/', '-').replace(':', '-')
            except Exception:
                # Fallback timestamp if datetime fails
                safe_timestamp = "unknown_date_00-00"
            
            return f'{safe_topic}_{safe_timestamp}.md'
            
        except Exception:
            # Ultimate fallback if everything fails
            return "report_unknown.md"