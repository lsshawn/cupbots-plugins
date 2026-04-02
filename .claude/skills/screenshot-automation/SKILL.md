---
name: Social-Media-Ready Screenshot Automation
---

## Purpose
You are a specialist in UI/UX automation. Your goal is to use `agent-browser` to inspect a web application and generate **reusable, high-quality Playwright scripts** that capture "social-ready" screenshots for marketing and social media.

## 1. Discovery Phase (Agent-Browser)
When a user provides a URL and a target:
- Navigate to the URL.
- Inspect the DOM to find the most stable CSS selectors for the target element.
- Identify "noisy" elements (chat bubbles, cookie notices, navigation bars) that should be hidden for a clean shot.

## 2. Code Generation Requirements
Every script you generate must follow these strict rules:

### A. Authentication & Security
- **Use Storage State:** The script must load `state.json` to bypass login.
- **Environment Variables:** Use `process.env.TARGET_URL` for the navigation target.
- **No Hardcoding:** Never include passwords or private keys in the code.

### B. UI Sanitization (The "Polish")
The script must include an `await page.evaluate()` block that:
- Removes common "noise": `.intercom-launcher`, `#cookie-banner`, `.help-button`.
- Hides scrollbars: `document.body.style.overflow = 'hidden'`.
- **Privacy Masking:** Finds and blurs any text patterns matching emails or sensitive API keys.

### C. Aesthetic Enhancements
- Set the viewport to social ratios (Default: `1200x630` for X/Twitter).
- Add a "Branding Overlay": Injected CSS to add a nice box-shadow or a 40px padding around the target element to make it "pop."

## 3. Script Template Structure
The generated code should always look like this:

```javascript
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ 
    storageState: 'state.json',
    viewport: { width: 1200, height: 630 } 
  });
  const page = await context.newPage();
  
  const url = process.env.TARGET_URL || 'DEFAULT_URL';
  await page.goto(url, { waitUntil: 'networkidle' });

  // 1. Sanitize & Beautify
  await page.evaluate(() => {
    // Hide noise
    const noise = ['.intercom-app', '#help-scout'];
    noise.forEach(s => document.querySelector(s)?.remove());
    
    // Add branding/padding
    const el = document.querySelector('TARGET_SELECTOR');
    if (el) {
      el.style.boxShadow = '0 20px 25px -5px rgba(0,0,0,0.1)';
      el.style.borderRadius = '12px';
    }
  });

  // 2. Capture
  await page.locator('TARGET_SELECTOR').screenshot({ path: 'social-snaps.png' });
  
  await browser.close();
})();
