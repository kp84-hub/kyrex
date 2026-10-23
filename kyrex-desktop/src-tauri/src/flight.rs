//! Presentation-only motion. Coordinates are physical pixels inside one work
//! area; this never observes another application or submits a Kyrex task.

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Point {
    pub x: f64,
    pub y: f64,
}

#[derive(Clone, Copy)]
pub struct Bounds {
    pub left: f64,
    pub top: f64,
    pub right: f64,
    pub bottom: f64,
    pub scale: f64,
}

impl Bounds {
    pub fn new(work: (i32, i32, u32, u32), size: (u32, u32), scale: f64) -> Self {
        let (x, y, width, height) = work;
        Self {
            left: f64::from(x),
            top: f64::from(y),
            right: f64::from(x) + f64::from(width.saturating_sub(size.0)),
            bottom: f64::from(y) + f64::from(height.saturating_sub(size.1)),
            scale: scale.clamp(0.5, 4.0),
        }
    }

    fn contain(self, p: Point) -> Point {
        Point {
            x: p.x.clamp(self.left, self.right),
            y: p.y.clamp(self.top, self.bottom),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum Stage {
    Cruise,
    Boost,
    Bonk,
    Sleep,
    Wake,
}

pub struct Frame {
    pub position: Point,
    pub mood: &'static str,
}

pub struct Flight {
    position: Option<Point>,
    stage: Stage,
    age: f64,
    flown: f64,
    boosted: bool,
    waypoint: usize,
}

impl Default for Flight {
    fn default() -> Self {
        Self {
            position: None,
            stage: Stage::Cruise,
            age: 0.0,
            flown: 0.0,
            boosted: false,
            waypoint: 0,
        }
    }
}

impl Flight {
    fn enter(&mut self, stage: Stage) {
        self.stage = stage;
        self.age = 0.0;
    }

    // A bounded delta prevents jumps after suspend or a busy event loop.
    // Paused time never counts as flying time.
    pub fn step(
        &mut self,
        seconds: f64,
        actual: Point,
        bounds: Bounds,
        enabled: bool,
        paused: bool,
    ) -> Frame {
        let dt = seconds.clamp(0.0, 0.1);
        if !enabled {
            *self = Self::default();
            return Frame {
                position: actual,
                mood: "idle",
            };
        }
        if paused {
            self.position = Some(actual);
            if matches!(self.stage, Stage::Sleep | Stage::Boost | Stage::Bonk) {
                self.flown = 0.0;
                self.boosted = false;
                self.enter(Stage::Wake);
            }
            if self.stage == Stage::Wake {
                self.age += dt;
                if self.age >= 0.8 {
                    self.enter(Stage::Cruise);
                }
            } else {
                self.age = 0.0;
            }
            return Frame {
                position: actual,
                mood: if self.stage == Stage::Wake {
                    "waking"
                } else {
                    "idle"
                },
            };
        }
        let mut p = self.position.unwrap_or(actual);
        // Native dragging, display changes and Reset position may relocate us.
        if (p.x.round() - actual.x).abs() > 2.0 || (p.y.round() - actual.y).abs() > 2.0 {
            p = actual;
        }
        p = bounds.contain(p);
        self.age += dt;
        if matches!(self.stage, Stage::Cruise | Stage::Boost | Stage::Bonk) {
            self.flown += dt;
        }
        let mood = match self.stage {
            Stage::Cruise => {
                if self.flown >= 65.0 {
                    self.enter(Stage::Sleep);
                    "sleeping"
                } else if !self.boosted && self.flown >= 24.0 {
                    self.boosted = true;
                    self.enter(Stage::Boost);
                    "boost"
                } else {
                    const STOPS: [(f64, f64); 5] = [
                        (0.18, 0.30),
                        (0.80, 0.62),
                        (0.55, 0.20),
                        (0.22, 0.76),
                        (0.78, 0.38),
                    ];
                    let (x, y) = STOPS[self.waypoint % STOPS.len()];
                    let dx = bounds.left + (bounds.right - bounds.left) * x - p.x;
                    let dy = bounds.top + (bounds.bottom - bounds.top) * y - p.y;
                    let distance = dx.hypot(dy);
                    let travel = (52.0 * bounds.scale * dt * self.age.min(1.0)).min(distance);
                    if distance > 0.0 {
                        p.x += dx / distance * travel;
                        p.y += dy / distance * travel;
                    }
                    if distance < 3.0 * bounds.scale || self.age >= 12.0 {
                        self.waypoint += 1;
                        self.age = 0.0;
                    }
                    if dx < 0.0 {
                        "flying-left"
                    } else {
                        "flying-right"
                    }
                }
            }
            Stage::Boost => {
                p.y =
                    (p.y - (80.0 + self.age.min(1.0) * 260.0) * bounds.scale * dt).max(bounds.top);
                if p.y <= bounds.top {
                    self.enter(Stage::Bonk);
                    "bonk"
                } else {
                    "boost"
                }
            }
            Stage::Bonk => {
                // A small recoil stays fully inside the display work area.
                p.y = (bounds.top + self.age.min(0.5) * 26.0 * bounds.scale).min(bounds.bottom);
                if self.age >= 1.4 {
                    self.enter(Stage::Cruise);
                }
                "bonk"
            }
            Stage::Sleep => {
                if self.age >= 25.0 {
                    self.flown = 0.0;
                    self.boosted = false;
                    self.enter(Stage::Wake);
                    "waking"
                } else {
                    "sleeping"
                }
            }
            Stage::Wake => {
                if self.age >= 0.8 {
                    self.enter(Stage::Cruise);
                }
                "waking"
            }
        };
        p = bounds.contain(p);
        self.position = Some(p);
        Frame { position: p, mood }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bounds() -> Bounds {
        Bounds::new((-1920, -120, 1920, 1040), (126, 126), 1.5)
    }
    fn start() -> Point {
        Point {
            x: -250.0,
            y: 650.0,
        }
    }

    #[test]
    fn flies_boosts_bonks_and_sleeps_inside_negative_monitor_work_area() {
        let mut flight = Flight::default();
        let b = bounds();
        let mut p = start();
        let mut seen = std::collections::HashSet::new();
        for _ in 0..1900 {
            let frame = flight.step(0.05, p, b, true, false);
            assert!(frame.position.x >= b.left && frame.position.x <= b.right);
            assert!(frame.position.y >= b.top && frame.position.y <= b.bottom);
            seen.insert(frame.mood);
            p = Point {
                x: frame.position.x.round(),
                y: frame.position.y.round(),
            };
        }
        for expected in [
            "flying-left",
            "flying-right",
            "boost",
            "bonk",
            "sleeping",
            "waking",
        ] {
            assert!(seen.contains(expected), "missing {expected}");
        }
        assert_ne!(p, start());
    }

    #[test]
    fn hover_wakes_and_holds_without_moving_or_consuming_flight_time() {
        let mut flight = Flight::default();
        flight.enter(Stage::Sleep);
        let p = start();
        for i in 0..400 {
            let frame = flight.step(0.05, p, bounds(), true, true);
            assert_eq!(frame.position, p);
            assert_eq!(frame.mood, if i < 15 { "waking" } else { "idle" });
        }
        assert_eq!(flight.flown, 0.0);
        assert_ne!(flight.step(0.05, p, bounds(), true, false).position, p);
    }

    #[test]
    fn disabled_reduced_motion_hidden_chat_and_settings_stay_put() {
        for (enabled, paused) in [(false, false), (true, true)] {
            let mut flight = Flight::default();
            for _ in 0..2000 {
                let frame = flight.step(0.05, start(), bounds(), enabled, paused);
                assert_eq!(frame.position, start());
                assert_eq!(frame.mood, "idle");
            }
        }
    }

    #[test]
    fn suspend_drag_and_tiny_work_areas_do_not_jump_or_escape() {
        let mut flight = Flight::default();
        let p = start();
        let frame = flight.step(600.0, p, bounds(), true, false);
        assert!((frame.position.x - p.x).hypot(frame.position.y - p.y) <= 8.0);
        let dragged = Point {
            x: -1500.0,
            y: 500.0,
        };
        assert_eq!(
            flight.step(0.05, dragged, bounds(), true, true).position,
            dragged
        );
        let frame = flight.step(0.05, dragged, bounds(), true, false);
        assert!((frame.position.x - dragged.x).hypot(frame.position.y - dragged.y) < 8.0);
        let tiny = Bounds::new((100, 50, 60, 40), (84, 84), 1.0);
        let frame = flight.step(0.05, dragged, tiny, true, false);
        assert_eq!(frame.position, Point { x: 100.0, y: 50.0 });
    }
}
