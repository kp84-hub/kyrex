use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use rand::{rngs::OsRng, RngCore};
use sha2::{Digest, Sha256};
use std::time::{Duration, Instant};
use url::Url;

pub const REDIRECT: &str = "kyrex-chat://auth/callback";

pub fn chat_origin(raw: &str) -> Result<Url, &'static str> {
    let url = Url::parse(raw).map_err(|_| "Invalid Chat address")?;
    if url.scheme() != "https"
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err("Chat address must be an HTTPS origin without a path or credentials");
    }
    Ok(url)
}

pub fn same_origin(origin: &Url, url: &Url) -> bool {
    origin.origin() == url.origin() && url.username().is_empty() && url.password().is_none()
}

// Never pass custom schemes, file paths, javascript or embedded credentials to
// the OS opener. Native sign-in uses a separately constructed trusted URL.
pub fn external_link(url: &Url) -> bool {
    url.scheme() == "https"
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
}

pub fn bubble_url(origin: &Url) -> Url {
    let mut url = origin.clone();
    url.query_pairs_mut().append_pair("surface", "kbot");
    url
}

pub fn bubble_close(origin: &Url, url: &Url) -> bool {
    same_origin(origin, url)
        && url.path() == "/__companion__/hide"
        && url.query().is_none()
        && url.fragment().is_none()
}

// These fixed presentation states cannot carry messages, commands or URLs.
pub fn mascot_phase(title: &str) -> Option<&str> {
    match title.strip_prefix("Kbot|")? {
        phase @ ("idle" | "thinking" | "replying" | "listening" | "speaking") => Some(phase),
        _ => None,
    }
}

#[derive(Debug)]
pub struct BubblePlacement {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
    pub side: &'static str,
    pub tail_y: f64,
}

// All coordinates are physical pixels. The work area excludes the taskbar;
// the result works with negative monitor coordinates and mixed display DPI.
pub fn bubble_placement(
    icon: (i32, i32, u32, u32),
    work: (i32, i32, u32, u32),
    scale: f64,
) -> BubblePlacement {
    let scale = if scale.is_finite() && scale > 0.0 {
        scale
    } else {
        1.0
    };
    let work_width = work.2.clamp(1, i32::MAX as u32) as i32;
    let work_height = work.3.clamp(1, i32::MAX as u32) as i32;
    let margin = ((8.0 * scale).round() as i32).min(work_width.min(work_height) / 4);
    let width = ((320.0 * scale).round() as i32).clamp(1, work_width - 2 * margin);
    let height = ((300.0 * scale).round() as i32).clamp(1, work_height - 2 * margin);
    let left_space = icon.0.saturating_sub(work.0);
    let right_space = work
        .0
        .saturating_add(work_width)
        .saturating_sub(icon.0)
        .saturating_sub(icon.2 as i32);
    let left = left_space >= width || left_space > right_space;
    let overlap = (6.0 * scale).round() as i32;
    let desired_x = if left {
        icon.0 - width + overlap
    } else {
        icon.0 + icon.2 as i32 - overlap
    };
    let center_y = icon.1 + icon.3 as i32 / 2;
    let x = desired_x.clamp(work.0 + margin, work.0 + work_width - margin - width);
    let y = (center_y - height + (50.0 * scale) as i32)
        .clamp(work.1 + margin, work.1 + work_height - margin - height);
    BubblePlacement {
        x,
        y,
        width: width as u32,
        height: height as u32,
        side: if left { "right" } else { "left" },
        tail_y: ((center_y - y) as f64 / scale)
            .clamp(28.0, (height as f64 / scale - 28.0).max(28.0)),
    }
}

pub struct PendingLogin {
    pub state: String,
    pub verifier: String,
    created: Instant,
}

fn random(bytes: usize) -> String {
    let mut value = vec![0; bytes];
    OsRng.fill_bytes(&mut value);
    URL_SAFE_NO_PAD.encode(value)
}

impl PendingLogin {
    pub fn new() -> Self {
        Self {
            state: random(32),
            verifier: random(48),
            created: Instant::now(),
        }
    }

    pub fn start_url(&self, origin: &Url) -> Url {
        let mut url = origin.join("/auth/desktop/start").unwrap();
        url.query_pairs_mut()
            .append_pair("state", &self.state)
            .append_pair("redirect_uri", REDIRECT)
            .append_pair(
                "code_challenge",
                &URL_SAFE_NO_PAD.encode(Sha256::digest(self.verifier.as_bytes())),
            )
            .append_pair("code_challenge_method", "S256");
        url
    }

    pub fn callback_code(&self, url: &Url) -> Result<String, &'static str> {
        if url.scheme() != "kyrex-chat"
            || url.host_str() != Some("auth")
            || url.path() != "/callback"
            || url.port().is_some()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some()
            || self.created.elapsed() > Duration::from_secs(600)
        {
            return Err("Sign-in link is invalid or expired. Start sign-in again.");
        }
        let pairs: Vec<_> = url.query_pairs().collect();
        if pairs.len() != 2
            || pairs.iter().filter(|(k, _)| k == "state").count() != 1
            || pairs.iter().filter(|(k, _)| k == "code").count() != 1
        {
            return Err("Invalid sign-in response. Start sign-in again.");
        }
        let state = pairs.iter().find(|(k, _)| k == "state").unwrap().1.as_ref();
        let code = pairs.iter().find(|(k, _)| k == "code").unwrap().1.as_ref();
        if state != self.state || code.is_empty() || code.len() > 256 {
            return Err("Sign-in response did not match this request. Start sign-in again.");
        }
        Ok(code.to_owned())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bubble_controls_are_origin_bound_and_phases_are_data_only() {
        let origin = chat_origin("https://chat.example").unwrap();
        assert_eq!(
            bubble_url(&origin).as_str(),
            "https://chat.example/?surface=kbot"
        );
        assert!(bubble_close(
            &origin,
            &origin.join("/__companion__/hide").unwrap()
        ));
        for raw in [
            "https://other.example/__companion__/hide",
            "https://chat.example/__companion__/hide?next=evil",
            "https://chat.example/__companion__/hide#x",
            "https://chat.example/api/tasks/cancel",
        ] {
            assert!(!bubble_close(&origin, &Url::parse(raw).unwrap()));
        }
        assert_eq!(mascot_phase("Kbot|speaking"), Some("speaking"));
        assert_eq!(mascot_phase("Kbot|replying"), Some("replying"));
        assert_eq!(mascot_phase("Kbot|https://other.example"), None);
        assert_eq!(mascot_phase("Kbot|speaking; alert(1)"), None);
    }

    #[test]
    fn bubble_stays_in_work_area_at_corners_and_mixed_dpi() {
        for (work, scale) in [
            ((0, 0, 1920, 1040), 1.0),
            ((-1920, -200, 1920, 1200), 1.5),
            ((0, 0, 300, 240), 1.0),
        ] {
            for (x, y) in [
                (work.0, work.1),
                (work.0 + work.2 as i32 - 84, work.1 + work.3 as i32 - 84),
            ] {
                let p = bubble_placement((x, y, 84, 84), work, scale);
                assert!(p.x >= work.0 && p.y >= work.1);
                assert!(p.x + p.width as i32 <= work.0 + work.2 as i32);
                assert!(p.y + p.height as i32 <= work.1 + work.3 as i32);
                assert!(p.tail_y >= 28.0 && p.tail_y <= p.height as f64 / scale - 28.0);
            }
        }
        let left = bubble_placement((20, 300, 84, 84), (0, 0, 1920, 1040), 1.0);
        assert_eq!(left.side, "left");
        assert!(left.x > 20);
        let right = bubble_placement((1700, 300, 84, 84), (0, 0, 1920, 1040), 1.0);
        assert_eq!(right.side, "right");
        assert!(right.x < 1700);
    }

    #[test]
    fn origins_are_exact_and_external_protocols_are_bounded() {
        let origin = chat_origin("https://chat.example").unwrap();
        for raw in [
            "http://chat.example",
            "https://chat.example/path",
            "https://a@chat.example",
            "https://chat.example?x=y",
        ] {
            assert!(chat_origin(raw).is_err());
        }
        assert!(same_origin(&origin, &origin.join("/api/me").unwrap()));
        for raw in [
            "https://chat.example.evil/path",
            "https://chat.example:444",
            "https://a@chat.example/",
        ] {
            assert!(!same_origin(&origin, &Url::parse(raw).unwrap()));
        }
        for raw in [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "ms-msdt:/anything",
            "http://localhost",
            "https://user:password@example.com",
        ] {
            assert!(!external_link(&Url::parse(raw).unwrap()));
        }
    }

    #[test]
    fn callback_requires_live_exact_state_and_unique_parameters() {
        let mut pending = PendingLogin::new();
        let callback = format!("{REDIRECT}?code=opaque&state={}", pending.state);
        assert_eq!(
            pending
                .callback_code(&Url::parse(&callback).unwrap())
                .unwrap(),
            "opaque"
        );
        for raw in [
            callback.replace("opaque", ""),
            callback.replace("auth/callback", "evil/callback"),
            callback.replace("kyrex-chat:", "kyrex:"),
            format!("{callback}&code=duplicate"),
            format!("{callback}#fragment"),
            callback.replace(&pending.state, "other-attempt"),
        ] {
            assert!(pending.callback_code(&Url::parse(&raw).unwrap()).is_err());
        }
        pending.created = Instant::now() - Duration::from_secs(601);
        assert!(pending
            .callback_code(&Url::parse(&callback).unwrap())
            .is_err());
    }

    #[test]
    fn login_uses_s256_and_fresh_random_values() {
        let a = PendingLogin::new();
        let b = PendingLogin::new();
        assert_ne!(a.state, b.state);
        assert_ne!(a.verifier, b.verifier);
        let url = a.start_url(&chat_origin("https://chat.example").unwrap());
        let pairs: std::collections::HashMap<_, _> = url.query_pairs().collect();
        assert_eq!(pairs["redirect_uri"], REDIRECT);
        assert_eq!(pairs["code_challenge_method"], "S256");
        assert_ne!(pairs["code_challenge"], a.verifier);
    }
}
