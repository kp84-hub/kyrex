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
