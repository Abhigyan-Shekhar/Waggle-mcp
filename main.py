# src/waggle/auth.py

from datetime import datetime, timezone

def iso_now() -> str:
    """
    Returns the current UTC time as an ISO 8601 formatted string.
    
    This function replaces the deprecated datetime.utcnow().isoformat() + 'Z'
    pattern with the recommended datetime.now(timezone.utc).isoformat() approach.
    
    Returns:
        str: Current UTC time in ISO 8601 format, e.g., '2024-01-15T10:30:00.123456+00:00'
    """
    try:
        # Get current UTC time using timezone-aware datetime
        current_utc_time = datetime.now(timezone.utc)
        
        # Convert to ISO 8601 format
        # Note: This produces '+00:00' instead of 'Z' for UTC timezone indicator
        return current_utc_time.isoformat()
    except Exception as e:
        # Handle any unexpected errors gracefully
        raise RuntimeError(f"Failed to generate UTC ISO timestamp: {e}") from e


# Test code
if __name__ == "__main__":
    import warnings
    import unittest
    
    class TestISONow(unittest.TestCase):
        """Test cases for iso_now() function."""
        
        def test_returns_correct_utc_iso_format(self):
            """Test that iso_now() returns a valid UTC ISO format string."""
            result = iso_now()
            
            # Check that result is a string
            self.assertIsInstance(result, str)
            
            # Check that result contains UTC timezone indicator
            self.assertIn('+00:00', result, "UTC timezone indicator '+00:00' should be present")
            
            # Check that result can be parsed back to datetime
            parsed_time = datetime.fromisoformat(result)
            self.assertIsNotNone(parsed_time, "Result should be a valid ISO datetime string")
            
            # Verify it's UTC time
            self.assertEqual(parsed_time.tzinfo, timezone.utc, "Timezone should be UTC")
        
        def test_no_deprecation_warnings(self):
            """Test that no deprecation warnings are raised from datetime.utcnow()."""
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                
                # Call the function
                result = iso_now()
                
                # Check for any DeprecationWarning related to datetime.utcnow
                deprecation_warnings = [
                    warning for warning in w 
                    if issubclass(warning.category, DeprecationWarning) 
                    and 'utcnow' in str(warning.message).lower()
                ]
                
                self.assertEqual(
                    len(deprecation_warnings), 0,
                    f"Found {len(deprecation_warnings)} deprecation warnings related to datetime.utcnow(): {deprecation_warnings}"
                )
        
        def test_returns_consistent_format(self):
            """Test that iso_now() returns consistent ISO format."""
            results = [iso_now() for _ in range(10)]
            
            # All results should have the same format
            for result in results:
                self.assertTrue(result.endswith('+00:00'), 
                              f"Result should end with '+00:00', got: {result}")
                self.assertIn('T', result, "Result should contain 'T' separator")
        
        def test_time_is_recent(self):
            """Test that the returned time is close to current time."""
            import time
            
            before = datetime.now(timezone.utc)
            time.sleep(0.1)  # Small delay
            result = iso_now()
            after = datetime.now(timezone.utc)
            
            parsed_result = datetime.fromisoformat(result)
            
            # The result should be between before and after times
            self.assertGreaterEqual(parsed_result, before, 
                                  "Result time should be >= time before call")
            self.assertLessEqual(parsed_result, after, 
                               "Result time should be <= time after call")
    
    # Run tests
    unittest.main(verbosity=2)