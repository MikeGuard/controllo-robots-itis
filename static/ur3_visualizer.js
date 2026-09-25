/**
 * Precision 3D Universal Robots UR3 Visualizer (Three.js)
 * Accurately models UR3 kinematics using exact Denavit-Hartenberg frames:
 * - d1 = 0.1519m (Base to Shoulder axis)
 * - a2 = -0.24365m (Upper Arm link)
 * - a3 = -0.21325m (Forearm link)
 * - d4 = 0.11235m (Wrist 1 cross offset)
 * - d5 = 0.08535m (Wrist 2 cross offset)
 * - d6 = 0.0819m (Wrist 3 / Tool Flange offset)
 *
 * Visual aesthetics match the real UR3 photograph:
 * - Industrial slate gray cast joints
 * - Matte off-white powder-coated cylindrical link tubes
 * - Dark charcoal accent collar rings
 * - Signature pale pastel cyan-blue domed caps
 * - Machined aluminum tool flange with center bore
 */

class UR3Visualizer {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        // Safe home pose: [-90°, -90°, -90°, -90°, 90°, 0°]
        this.homePose = [-Math.PI / 2, -Math.PI / 2, -Math.PI / 2, -Math.PI / 2, Math.PI / 2, 0.0];
        // Photo pose matching the uploaded image
        this.photoPose = [-0.61, -1.31, 1.83, -2.09, -1.57, 0.0];

        this.currentQ = [...this.homePose];
        this.targetQ = [...this.homePose];
        this.isSimulating = false;
        this.simTimer = null;
        this.currentModel = 'ur';

        this.initThree();
        this.buildMaterials();
        this.buildDHRobotModel();
        this.applyJointAngles(this.currentQ);

        this.animate = this.animate.bind(this);
        requestAnimationFrame(this.animate);
    }

    initThree() {
        const width = this.container.clientWidth || 400;
        const height = this.container.clientHeight || 300;

        // Scene
        this.scene = new THREE.Scene();
        this.scene.background = new THREE.Color(0x111622);

        // Camera
        this.camera = new THREE.PerspectiveCamera(38, width / height, 0.05, 50);
        this.camera.position.set(0.95, 0.65, 0.95);

        // WebGL Renderer
        this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
        this.renderer.setSize(width, height);
        this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
        this.renderer.shadowMap.enabled = true;
        this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
        this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
        this.renderer.toneMappingExposure = 1.2;
        this.container.appendChild(this.renderer.domElement);

        // OrbitControls
        this.controls = new THREE.OrbitControls(this.camera, this.renderer.domElement);
        this.controls.enableDamping = true;
        this.controls.dampingFactor = 0.08;
        this.controls.target.set(0, 0.28, 0);
        this.controls.maxPolarAngle = Math.PI / 2 + 0.02; // Restrict camera from going under table
        this.controls.minDistance = 0.35;
        this.controls.maxDistance = 2.8;
        this.controls.update();

        // Lighting
        const hemiLight = new THREE.HemisphereLight(0xffffff, 0x1e293b, 0.7);
        this.scene.add(hemiLight);

        const keyLight = new THREE.DirectionalLight(0xffffff, 1.4);
        keyLight.position.set(1.8, 3.0, 2.2);
        keyLight.castShadow = true;
        keyLight.shadow.mapSize.width = 1024;
        keyLight.shadow.mapSize.height = 1024;
        keyLight.shadow.bias = -0.0003;
        this.scene.add(keyLight);

        const rimLight = new THREE.DirectionalLight(0xaad8f5, 0.7);
        rimLight.position.set(-2.5, 2.0, -2.0);
        this.scene.add(rimLight);

        const fillLight = new THREE.DirectionalLight(0xffffff, 0.4);
        fillLight.position.set(-1.0, 0.5, 2.5);
        this.scene.add(fillLight);

        // Floor Pedestal & Grid
        const floorGeo = new THREE.PlaneGeometry(3.0, 3.0);
        const floorMat = new THREE.MeshStandardMaterial({
            color: 0x0e131d,
            roughness: 0.85,
            metalness: 0.1
        });
        const floor = new THREE.Mesh(floorGeo, floorMat);
        floor.rotation.x = -Math.PI / 2;
        floor.receiveShadow = true;
        this.scene.add(floor);

        const grid = new THREE.GridHelper(2.0, 20, 0x3b485d, 0x1e2638);
        grid.position.y = 0.001;
        this.scene.add(grid);

        // Responsive resize handlers
        const handleResize = () => {
            if (!this.container) return;
            const w = this.container.clientWidth;
            const h = this.container.clientHeight;
            if (w > 0 && h > 0 && this.renderer && this.camera) {
                this.camera.aspect = w / h;
                this.camera.updateProjectionMatrix();
                this.renderer.setSize(w, h);
            }
        };

        window.addEventListener('resize', handleResize);
        if (window.ResizeObserver && this.container) {
            const ro = new ResizeObserver(handleResize);
            ro.observe(this.container);
        }
    }

    buildMaterials() {
        // Authentic UR3 industrial color palette
        this.matSlateGray = new THREE.MeshStandardMaterial({
            color: 0x505763,
            metalness: 0.35,
            roughness: 0.42
        });

        this.matOffWhite = new THREE.MeshStandardMaterial({
            color: 0xe6e9ee,
            metalness: 0.12,
            roughness: 0.38
        });

        this.matDarkRing = new THREE.MeshStandardMaterial({
            color: 0x16181d,
            metalness: 0.75,
            roughness: 0.28
        });

        this.matBlueCap = new THREE.MeshStandardMaterial({
            color: 0x9dc6de, // Signature pale cyan-blue UR cap
            metalness: 0.18,
            roughness: 0.38
        });

        this.matAluminumFlange = new THREE.MeshStandardMaterial({
            color: 0xd6dae2,
            metalness: 0.88,
            roughness: 0.22
        });
    }

    // Helper to build a clean domed cap matching the photo
    createDomedCap(radius, capThickness) {
        const grp = new THREE.Group();
        const rim = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, capThickness, 36), this.matBlueCap);
        rim.castShadow = true;
        rim.position.y = capThickness / 2;
        grp.add(rim);

        const dome = new THREE.Mesh(new THREE.SphereGeometry(radius * 0.99, 36, 16, 0, Math.PI * 2, 0, Math.PI / 2), this.matBlueCap);
        dome.scale.set(1, 0.42, 1);
        dome.position.y = capThickness;
        dome.castShadow = true;
        grp.add(dome);

        return grp;
    }

    buildDHRobotModel() {
        // Root group placed on ground with coordinates aligned to robotics standard:
        // Three.js world has Y-up. By rotating robotRoot by -90° around X:
        // Local +Z is UP (Robot Base Z), Local +X is FORWARD, Local +Y is LEFT.
        this.robotRoot = new THREE.Group();
        this.robotRoot.rotation.x = -Math.PI / 2;
        this.scene.add(this.robotRoot);

        // 1. Static Table Mounting Base
        const baseMount = new THREE.Group();
        this.robotRoot.add(baseMount);

        // Flared conical skirt
        const skirt = new THREE.Mesh(
            new THREE.CylinderGeometry(0.056, 0.078, 0.038, 48),
            this.matSlateGray
        );
        skirt.rotation.x = Math.PI / 2;
        skirt.position.z = 0.019;
        skirt.castShadow = true;
        skirt.receiveShadow = true;
        baseMount.add(skirt);

        // 4 mounting tabs on perimeter
        for (let i = 0; i < 4; i++) {
            const ang = (i * Math.PI) / 2 + Math.PI / 4;
            const tab = new THREE.Mesh(
                new THREE.CylinderGeometry(0.009, 0.009, 0.018, 16),
                this.matSlateGray
            );
            tab.rotation.x = Math.PI / 2;
            tab.position.set(Math.cos(ang) * 0.072, Math.sin(ang) * 0.072, 0.009);
            tab.castShadow = true;
            baseMount.add(tab);
        }

        // Base black accent collar
        const baseRing = new THREE.Mesh(
            new THREE.CylinderGeometry(0.0565, 0.0565, 0.008, 48),
            this.matDarkRing
        );
        baseRing.rotation.x = Math.PI / 2;
        baseRing.position.z = 0.038;
        baseMount.add(baseRing);

        // --- Joint 0 (Base Pan) ---
        // Rotates around local Z (vertical)
        this.j0 = new THREE.Group();
        this.robotRoot.add(this.j0);

        // Lower base pan housing cylinder
        const basePanCyl = new THREE.Mesh(
            new THREE.CylinderGeometry(0.052, 0.052, 0.075, 48),
            this.matSlateGray
        );
        basePanCyl.rotation.x = Math.PI / 2;
        basePanCyl.position.z = 0.038 + 0.0375;
        basePanCyl.castShadow = true;
        this.j0.add(basePanCyl);

        // Signature pale blue cap on top of base pan
        const basePanCap = this.createDomedCap(0.0515, 0.014);
        basePanCap.rotation.x = -Math.PI / 2;
        basePanCap.position.z = 0.038 + 0.075;
        this.j0.add(basePanCap);

        // Black accent seam ring
        const panSeam = new THREE.Mesh(
            new THREE.CylinderGeometry(0.0525, 0.0525, 0.006, 48),
            this.matDarkRing
        );
        panSeam.rotation.x = Math.PI / 2;
        panSeam.position.z = 0.038 + 0.074;
        this.j0.add(panSeam);

        // --- Joint 1 (Shoulder) ---
        // DH: d1 = 0.1519 along Z, alpha1 = +pi/2 around X
        const d1 = 0.1519;
        this.j1_frame = new THREE.Group();
        this.j1_frame.position.set(0, 0, d1);
        this.j1_frame.rotation.x = Math.PI / 2;
        this.j0.add(this.j1_frame);

        this.j1 = new THREE.Group();
        this.j1_frame.add(this.j1);

        // Shoulder joint horizontal housing cylinder
        const shoulderHousing = new THREE.Mesh(
            new THREE.CylinderGeometry(0.050, 0.050, 0.106, 48),
            this.matSlateGray
        );
        shoulderHousing.rotation.x = Math.PI / 2;
        shoulderHousing.position.z = 0.015;
        shoulderHousing.castShadow = true;
        this.j1.add(shoulderHousing);

        // Shoulder outer blue cap
        const shoulderCap = this.createDomedCap(0.0495, 0.016);
        shoulderCap.rotation.x = Math.PI / 2;
        shoulderCap.position.z = 0.015 + 0.053;
        this.j1.add(shoulderCap);

        // Inner black accent collar
        const shoulderInnerRing = new THREE.Mesh(
            new THREE.CylinderGeometry(0.0505, 0.0505, 0.008, 48),
            this.matDarkRing
        );
        shoulderInnerRing.rotation.x = Math.PI / 2;
        shoulderInnerRing.position.z = 0.015 - 0.053;
        this.j1.add(shoulderInnerRing);

        // --- Upper Arm Link ---
        // DH: a2 = -0.24365 along X
        const a2 = -0.24365;
        const upperArmLen = Math.abs(a2);

        const upperArmTube = new THREE.Mesh(
            new THREE.CylinderGeometry(0.034, 0.034, upperArmLen, 48),
            this.matOffWhite
        );
        upperArmTube.rotation.z = Math.PI / 2;
        upperArmTube.position.set(-upperArmLen / 2, 0, -0.038);
        upperArmTube.castShadow = true;
        this.j1.add(upperArmTube);

        // Upper arm collar rings
        const uRing1 = new THREE.Mesh(new THREE.CylinderGeometry(0.0355, 0.0355, 0.008, 48), this.matDarkRing);
        uRing1.rotation.z = Math.PI / 2;
        uRing1.position.set(-0.015, 0, -0.038);
        this.j1.add(uRing1);

        const uRing2 = new THREE.Mesh(new THREE.CylinderGeometry(0.0355, 0.0355, 0.008, 48), this.matDarkRing);
        uRing2.rotation.z = Math.PI / 2;
        uRing2.position.set(-upperArmLen + 0.015, 0, -0.038);
        this.j1.add(uRing2);

        // --- Joint 2 (Elbow) ---
        // DH: a2 along X, alpha2 = 0
        this.j2_frame = new THREE.Group();
        this.j2_frame.position.set(a2, 0, 0);
        this.j1.add(this.j2_frame);

        this.j2 = new THREE.Group();
        this.j2_frame.add(this.j2);

        // Elbow joint housing cylinder
        const elbowHousing = new THREE.Mesh(
            new THREE.CylinderGeometry(0.045, 0.045, 0.098, 48),
            this.matSlateGray
        );
        elbowHousing.rotation.x = Math.PI / 2;
        elbowHousing.position.z = -0.012;
        elbowHousing.castShadow = true;
        this.j2.add(elbowHousing);

        // Elbow outer blue cap
        const elbowCap = this.createDomedCap(0.0445, 0.016);
        elbowCap.rotation.x = -Math.PI / 2;
        elbowCap.position.z = -0.012 - 0.049;
        this.j2.add(elbowCap);

        const elbowInnerRing = new THREE.Mesh(
            new THREE.CylinderGeometry(0.0455, 0.0455, 0.008, 48),
            this.matDarkRing
        );
        elbowInnerRing.rotation.x = Math.PI / 2;
        elbowInnerRing.position.z = -0.012 + 0.049;
        this.j2.add(elbowInnerRing);

        // --- Forearm Link ---
        // DH: a3 = -0.21325 along X
        const a3 = -0.21325;
        const forearmLen = Math.abs(a3);

        const forearmTube = new THREE.Mesh(
            new THREE.CylinderGeometry(0.029, 0.029, forearmLen, 48),
            this.matOffWhite
        );
        forearmTube.rotation.z = Math.PI / 2;
        forearmTube.position.set(-forearmLen / 2, 0, 0.034);
        forearmTube.castShadow = true;
        this.j2.add(forearmTube);

        // Forearm collar rings
        const fRing1 = new THREE.Mesh(new THREE.CylinderGeometry(0.0305, 0.0305, 0.008, 48), this.matDarkRing);
        fRing1.rotation.z = Math.PI / 2;
        fRing1.position.set(-0.015, 0, 0.034);
        this.j2.add(fRing1);

        const fRing2 = new THREE.Mesh(new THREE.CylinderGeometry(0.0305, 0.0305, 0.008, 48), this.matDarkRing);
        fRing2.rotation.z = Math.PI / 2;
        fRing2.position.set(-forearmLen + 0.015, 0, 0.034);
        this.j2.add(fRing2);

        // --- Joint 3 (Wrist 1) ---
        // DH: a3 along X, alpha3 = 0
        this.j3_frame = new THREE.Group();
        this.j3_frame.position.set(a3, 0, 0);
        this.j2.add(this.j3_frame);

        this.j3 = new THREE.Group();
        this.j3_frame.add(this.j3);

        // Wrist 1 housing
        const w1Housing = new THREE.Mesh(
            new THREE.CylinderGeometry(0.038, 0.038, 0.082, 48),
            this.matSlateGray
        );
        w1Housing.rotation.x = Math.PI / 2;
        w1Housing.position.z = 0.056;
        w1Housing.castShadow = true;
        this.j3.add(w1Housing);

        const w1Cap = this.createDomedCap(0.0375, 0.014);
        w1Cap.rotation.x = Math.PI / 2;
        w1Cap.position.z = 0.056 + 0.041;
        this.j3.add(w1Cap);

        const w1Ring = new THREE.Mesh(new THREE.CylinderGeometry(0.0385, 0.0385, 0.006, 48), this.matDarkRing);
        w1Ring.rotation.x = Math.PI / 2;
        w1Ring.position.z = 0.056 - 0.041;
        this.j3.add(w1Ring);

        // --- Joint 4 (Wrist 2) ---
        // DH: d4 = 0.11235 along Z, alpha4 = +pi/2 around X
        const d4 = 0.11235;
        this.j4_frame = new THREE.Group();
        this.j4_frame.position.set(0, 0, d4);
        this.j4_frame.rotation.x = Math.PI / 2;
        this.j3.add(this.j4_frame);

        this.j4 = new THREE.Group();
        this.j4_frame.add(this.j4);

        // Wrist 2 housing
        const w2Housing = new THREE.Mesh(
            new THREE.CylinderGeometry(0.036, 0.036, 0.076, 48),
            this.matSlateGray
        );
        w2Housing.rotation.x = Math.PI / 2;
        w2Housing.position.z = 0.042;
        w2Housing.castShadow = true;
        this.j4.add(w2Housing);

        const w2Cap = this.createDomedCap(0.0355, 0.014);
        w2Cap.rotation.x = Math.PI / 2;
        w2Cap.position.z = 0.042 + 0.038;
        this.j4.add(w2Cap);

        const w2Ring = new THREE.Mesh(new THREE.CylinderGeometry(0.0365, 0.0365, 0.006, 48), this.matDarkRing);
        w2Ring.rotation.x = Math.PI / 2;
        w2Ring.position.z = 0.042 - 0.038;
        this.j4.add(w2Ring);

        // --- Joint 5 (Wrist 3 / Tool Flange) ---
        // DH: d5 = 0.08535 along Z, alpha5 = -pi/2 around X
        const d5 = 0.08535;
        this.j5_frame = new THREE.Group();
        this.j5_frame.position.set(0, 0, d5);
        this.j5_frame.rotation.x = -Math.PI / 2;
        this.j4.add(this.j5_frame);

        this.j5 = new THREE.Group();
        this.j5_frame.add(this.j5);

        // Wrist 3 housing
        const d6 = 0.0819;
        const w3Housing = new THREE.Mesh(
            new THREE.CylinderGeometry(0.033, 0.033, d6 * 0.75, 48),
            this.matSlateGray
        );
        w3Housing.rotation.x = Math.PI / 2;
        w3Housing.position.z = (d6 * 0.75) / 2;
        w3Housing.castShadow = true;
        this.j5.add(w3Housing);

        // White contrast collar band matching photo
        const toolCollar = new THREE.Mesh(
            new THREE.CylinderGeometry(0.0335, 0.0335, 0.014, 48),
            this.matOffWhite
        );
        toolCollar.rotation.x = Math.PI / 2;
        toolCollar.position.z = d6 * 0.62;
        this.j5.add(toolCollar);

        // Machined Aluminum Tool Mounting Flange
        const toolFlange = new THREE.Mesh(
            new THREE.CylinderGeometry(0.032, 0.032, 0.010, 48),
            this.matAluminumFlange
        );
        toolFlange.rotation.x = Math.PI / 2;
        toolFlange.position.z = d6 - 0.005;
        toolFlange.castShadow = true;
        this.j5.add(toolFlange);

        // Center index bore
        const centerBore = new THREE.Mesh(
            new THREE.CylinderGeometry(0.007, 0.007, 0.012, 24),
            this.matDarkRing
        );
        centerBore.rotation.x = Math.PI / 2;
        centerBore.position.z = d6 - 0.004;
        this.j5.add(centerBore);

        // 4 mounting screw dots
        for (let i = 0; i < 4; i++) {
            const ang = (i * Math.PI) / 2;
            const screw = new THREE.Mesh(
                new THREE.CylinderGeometry(0.0022, 0.0022, 0.011, 12),
                this.matDarkRing
            );
            screw.rotation.x = Math.PI / 2;
            screw.position.set(Math.cos(ang) * 0.020, Math.sin(ang) * 0.020, d6 - 0.004);
            this.j5.add(screw);
        }

        // Store active joint references for direct DH rotation
        this.joints = [this.j0, this.j1, this.j2, this.j3, this.j4, this.j5];
    }

    applyJointAngles(q) {
        if (!q || q.length !== 6 || !this.joints) return;
        // Direct Denavit-Hartenberg rotation around joint Z-axis:
        for (let i = 0; i < 6; i++) {
            if (this.joints[i]) {
                this.joints[i].rotation.z = q[i];
            }
        }
    }

    setJointAngles(q) {
        if (!q || q.length !== 6) return;
        for (let i = 0; i < 6; i++) {
            let target = q[i];
            // Normalize angular difference to [-pi, pi] to take shortest angular path
            let diff = target - this.currentQ[i];
            diff = ((diff + Math.PI) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) - Math.PI;
            this.targetQ[i] = this.currentQ[i] + diff;
        }
    }

    setPhotoPose() {
        this.setJointAngles(this.photoPose);
    }

    setHomePose() {
        this.setJointAngles(this.homePose);
    }

    setZeroPose() {
        this.setJointAngles(this.zeroPose);
    }

    resetCamera() {
        this.camera.position.set(0.95, 0.65, 0.95);
        this.controls.target.set(0, 0.28, 0);
        this.controls.update();
    }

    playTrajectory(waypoints, onStep, onComplete) {
        if (!waypoints || waypoints.length === 0) return;
        this.stopSimulation();

        // Auto-switch visualizer model if trajectory specifies robot model
        const modelHint = waypoints[0]?.robot_model;
        if (modelHint && modelHint !== this.currentModel) {
            this.setRobotModel(modelHint);
        }

        this.isSimulating = true;
        let index = 0;

        const executeStep = () => {
            if (!this.isSimulating) return;
            if (index >= waypoints.length) {
                this.isSimulating = false;
                if (onComplete) onComplete();
                return;
            }

            const wp = waypoints[index];
            index++;

            if (wp.q && wp.q.length === 6) {
                this.setJointAngles(wp.q);
            }

            if (onStep) {
                onStep(wp, index, waypoints.length);
            }

            // Intermediate trajectory steps run smoothly at ~45ms;
            // Final command goals pause slightly (~250ms) for clear visual feedback.
            const delay = wp.is_final ? 250 : 45;
            this.simTimer = setTimeout(executeStep, delay);
        };

        executeStep();
    }

    stopSimulation() {
        this.isSimulating = false;
        if (this.simTimer) {
            clearTimeout(this.simTimer);
            this.simTimer = null;
        }
    }

    animate() {
        requestAnimationFrame(this.animate);

        // Smooth cubic-like interpolation towards target joint angles
        let moved = false;
        for (let i = 0; i < 6; i++) {
            const diff = this.targetQ[i] - this.currentQ[i];
            if (Math.abs(diff) > 0.0003) {
                this.currentQ[i] += diff * 0.18;
                moved = true;
            } else {
                this.currentQ[i] = this.targetQ[i];
            }
        }

        if (moved) {
            this.applyJointAngles(this.currentQ);
            this.updateAngleDisplay();
        }

        this.controls.update();
        this.renderer.render(this.scene, this.camera);
    }

    updateAngleDisplay() {
        const text = this.currentQ.map(a => (a * 180 / Math.PI).toFixed(1) + "°").join(" | ");
        const disp = this.container.parentElement.querySelector(".ur3AngleReadout");
        if (disp) {
            disp.innerText = text;
        } else {
            const fallback = document.getElementById("ur3AngleReadout");
            if (fallback) fallback.innerText = text;
        }
    }

    setRobotModel(model) {
        if (!model) return;
        const normalized = model.toLowerCase();
        if (this.currentModel === normalized && this.robotRoot) return;
        this.currentModel = normalized;

        if (this.robotRoot) {
            this.scene.remove(this.robotRoot);
            this.robotRoot = null;
        }

        if (this.currentModel === 'niryo') {
            this.homePose = [0.0, 0.5, -1.25, 0.0, 0.0, 0.0];
            this.photoPose = [0.0, 0.35, -0.9, 0.0, 0.5, 0.0];
            this.zeroPose = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0];
            this.buildNiryoMaterials();
            this.buildNiryoModel();
        } else {
            this.homePose = [-Math.PI / 2, -Math.PI / 2, -Math.PI / 2, -Math.PI / 2, Math.PI / 2, 0.0];
            this.photoPose = [-0.61, -1.31, 1.83, -2.09, -1.57, 0.0];
            this.zeroPose = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0];
            this.buildMaterials();
            this.buildDHRobotModel();
        }

        this.currentQ = [...this.homePose];
        this.targetQ = [...this.homePose];
        this.applyJointAngles(this.currentQ);
        this.updateAngleDisplay();
    }

    buildNiryoMaterials() {
        this.matNiryoDark = new THREE.MeshStandardMaterial({
            color: 0x1e222b,
            metalness: 0.35,
            roughness: 0.55
        });
        this.matNiryoTeal = new THREE.MeshStandardMaterial({
            color: 0x00bcd4,
            metalness: 0.2,
            roughness: 0.35
        });
        this.matNiryoAccent = new THREE.MeshStandardMaterial({
            color: 0x374151,
            metalness: 0.45,
            roughness: 0.4
        });
        this.matNiryoMetal = new THREE.MeshStandardMaterial({
            color: 0xd1d5db,
            metalness: 0.85,
            roughness: 0.22
        });
        this.matNiryoGripper = new THREE.MeshStandardMaterial({
            color: 0x111827,
            metalness: 0.4,
            roughness: 0.65
        });
        this.matNiryoRubber = new THREE.MeshStandardMaterial({
            color: 0x0a0c10,
            metalness: 0.1,
            roughness: 0.9
        });
    }

    buildNiryoModel() {
        this.robotRoot = new THREE.Group();
        this.robotRoot.rotation.x = -Math.PI / 2;
        this.scene.add(this.robotRoot);

        // 1. Base Mount
        const baseMount = new THREE.Group();
        this.robotRoot.add(baseMount);

        const bBottom = new THREE.Mesh(
            new THREE.CylinderGeometry(0.082, 0.090, 0.020, 48),
            this.matNiryoDark
        );
        bBottom.rotation.x = Math.PI / 2;
        bBottom.position.z = 0.010;
        bBottom.castShadow = true;
        bBottom.receiveShadow = true;
        baseMount.add(bBottom);

        const bTealRing = new THREE.Mesh(
            new THREE.CylinderGeometry(0.083, 0.083, 0.005, 48),
            this.matNiryoTeal
        );
        bTealRing.rotation.x = Math.PI / 2;
        bTealRing.position.z = 0.022;
        baseMount.add(bTealRing);

        const bUpper = new THREE.Mesh(
            new THREE.CylinderGeometry(0.068, 0.080, 0.024, 48),
            this.matNiryoDark
        );
        bUpper.rotation.x = Math.PI / 2;
        bUpper.position.z = 0.034;
        bUpper.castShadow = true;
        baseMount.add(bUpper);

        // --- Joint 0 (Base Pan) ---
        this.j0 = new THREE.Group();
        this.robotRoot.add(this.j0);

        const j0Housing = new THREE.Mesh(
            new THREE.CylinderGeometry(0.065, 0.065, 0.065, 48),
            this.matNiryoDark
        );
        j0Housing.rotation.x = Math.PI / 2;
        j0Housing.position.z = 0.046 + 0.0325;
        j0Housing.castShadow = true;
        this.j0.add(j0Housing);

        const j0Teal = new THREE.Mesh(
            new THREE.CylinderGeometry(0.066, 0.066, 0.008, 48),
            this.matNiryoTeal
        );
        j0Teal.rotation.x = Math.PI / 2;
        j0Teal.position.z = 0.046 + 0.065;
        this.j0.add(j0Teal);

        // --- Joint 1 (Shoulder Pitch) ---
        // d1 = 0.130m along Z
        const d1 = 0.130;
        this.j1_frame = new THREE.Group();
        this.j1_frame.position.set(0, 0, d1);
        this.j1_frame.rotation.x = Math.PI / 2; // Z becomes horizontal pitch axis
        this.j0.add(this.j1_frame);

        this.j1 = new THREE.Group();
        this.j1_frame.add(this.j1);

        const j1Body = new THREE.Mesh(
            new THREE.CylinderGeometry(0.045, 0.045, 0.095, 48),
            this.matNiryoDark
        );
        j1Body.rotation.x = Math.PI / 2;
        j1Body.position.z = 0.0;
        j1Body.castShadow = true;
        this.j1.add(j1Body);

        const j1Cap1 = new THREE.Mesh(
            new THREE.CylinderGeometry(0.0455, 0.0455, 0.008, 36),
            this.matNiryoTeal
        );
        j1Cap1.rotation.x = Math.PI / 2;
        j1Cap1.position.z = 0.048;
        this.j1.add(j1Cap1);

        const j1Cap2 = new THREE.Mesh(
            new THREE.CylinderGeometry(0.0455, 0.0455, 0.008, 36),
            this.matNiryoTeal
        );
        j1Cap2.rotation.x = Math.PI / 2;
        j1Cap2.position.z = -0.048;
        this.j1.add(j1Cap2);

        // --- Upper Arm Link ---
        // Upper arm extends along local +Y (length L1 = 0.210m)
        const L1 = 0.210;
        const upperArmMesh = new THREE.Mesh(
            new THREE.BoxGeometry(0.048, L1, 0.052),
            this.matNiryoDark
        );
        upperArmMesh.position.set(0, L1 / 2, 0);
        upperArmMesh.castShadow = true;
        this.j1.add(upperArmMesh);

        const upperArmStripe = new THREE.Mesh(
            new THREE.BoxGeometry(0.050, L1 * 0.75, 0.012),
            this.matNiryoTeal
        );
        upperArmStripe.position.set(0, L1 / 2, 0.021);
        this.j1.add(upperArmStripe);

        // --- Joint 2 (Elbow Pitch) ---
        // At the top of upper arm: position (0, L1, 0), parallel pitch axis
        this.j2_frame = new THREE.Group();
        this.j2_frame.position.set(0, L1, 0);
        this.j1.add(this.j2_frame);

        this.j2 = new THREE.Group();
        this.j2_frame.add(this.j2);

        const elbowBody = new THREE.Mesh(
            new THREE.CylinderGeometry(0.038, 0.038, 0.080, 48),
            this.matNiryoDark
        );
        elbowBody.rotation.x = Math.PI / 2;
        elbowBody.position.z = 0.0;
        elbowBody.castShadow = true;
        this.j2.add(elbowBody);

        const elbowTeal = new THREE.Mesh(
            new THREE.CylinderGeometry(0.0385, 0.0385, 0.008, 36),
            this.matNiryoTeal
        );
        elbowTeal.rotation.x = Math.PI / 2;
        elbowTeal.position.z = -0.040;
        this.j2.add(elbowTeal);

        // --- Forearm Link ---
        // Extends along local +Y (length L2 = 0.190m)
        const L2 = 0.190;
        const forearmMesh = new THREE.Mesh(
            new THREE.BoxGeometry(0.038, L2, 0.042),
            this.matNiryoDark
        );
        forearmMesh.position.set(0, L2 / 2, 0);
        forearmMesh.castShadow = true;
        this.j2.add(forearmMesh);

        const forearmStripe = new THREE.Mesh(
            new THREE.BoxGeometry(0.040, L2 * 0.65, 0.008),
            this.matNiryoTeal
        );
        forearmStripe.position.set(0, L2 / 2, 0.017);
        this.j2.add(forearmStripe);

        // --- Joint 3 (Forearm Roll) ---
        // At the end of forearm: (0, L2, 0)
        // Rotate frame around X by -pi/2 so local Z points along the forearm axis (ROLL)
        this.j3_frame = new THREE.Group();
        this.j3_frame.position.set(0, L2, 0);
        this.j3_frame.rotation.x = -Math.PI / 2;
        this.j2.add(this.j3_frame);

        this.j3 = new THREE.Group();
        this.j3_frame.add(this.j3);

        const rollCollar = new THREE.Mesh(
            new THREE.CylinderGeometry(0.032, 0.032, 0.030, 36),
            this.matNiryoDark
        );
        rollCollar.position.z = 0.015;
        rollCollar.castShadow = true;
        this.j3.add(rollCollar);

        const rollRing = new THREE.Mesh(
            new THREE.CylinderGeometry(0.0325, 0.0325, 0.006, 36),
            this.matNiryoTeal
        );
        rollRing.position.z = 0.028;
        this.j3.add(rollRing);

        // --- Joint 4 (Wrist Pitch) ---
        // Positioned 0.045m along the forearm roll axis (Z)
        // Rotate frame around X by +pi/2 so local Z is the pitch hinge axis
        const d4 = 0.045;
        this.j4_frame = new THREE.Group();
        this.j4_frame.position.set(0, 0, d4);
        this.j4_frame.rotation.x = Math.PI / 2;
        this.j3.add(this.j4_frame);

        this.j4 = new THREE.Group();
        this.j4_frame.add(this.j4);

        const wPitchBody = new THREE.Mesh(
            new THREE.CylinderGeometry(0.027, 0.027, 0.055, 36),
            this.matNiryoDark
        );
        wPitchBody.rotation.x = Math.PI / 2;
        wPitchBody.castShadow = true;
        this.j4.add(wPitchBody);

        const wPitchTeal = new THREE.Mesh(
            new THREE.CylinderGeometry(0.0275, 0.0275, 0.006, 36),
            this.matNiryoTeal
        );
        wPitchTeal.rotation.x = Math.PI / 2;
        wPitchTeal.position.z = 0.028;
        this.j4.add(wPitchTeal);

        // --- Joint 5 (Tool Roll & Gripper) ---
        // Positioned 0.040m along the wrist, rotate around X by -pi/2 so local Z points out along tool
        const d5 = 0.040;
        this.j5_frame = new THREE.Group();
        this.j5_frame.position.set(0, 0, d5);
        this.j5_frame.rotation.x = -Math.PI / 2;
        this.j4.add(this.j5_frame);

        this.j5 = new THREE.Group();
        this.j5_frame.add(this.j5);

        // Tool Mount Flange
        const toolMount = new THREE.Mesh(
            new THREE.CylinderGeometry(0.025, 0.025, 0.015, 36),
            this.matNiryoDark
        );
        toolMount.position.z = 0.0075;
        toolMount.castShadow = true;
        this.j5.add(toolMount);

        const toolRing = new THREE.Mesh(
            new THREE.CylinderGeometry(0.0255, 0.0255, 0.005, 36),
            this.matNiryoMetal
        );
        toolRing.position.z = 0.015;
        this.j5.add(toolRing);

        // Gripper Body
        const gripperBase = new THREE.Mesh(
            new THREE.BoxGeometry(0.048, 0.022, 0.016),
            this.matNiryoGripper
        );
        gripperBase.position.z = 0.025;
        gripperBase.castShadow = true;
        this.j5.add(gripperBase);

        const gripperTealPlate = new THREE.Mesh(
            new THREE.BoxGeometry(0.044, 0.018, 0.003),
            this.matNiryoTeal
        );
        gripperTealPlate.position.z = 0.034;
        this.j5.add(gripperTealPlate);

        // Left Finger
        const fingerLeft = new THREE.Mesh(
            new THREE.BoxGeometry(0.007, 0.014, 0.030),
            this.matNiryoGripper
        );
        fingerLeft.position.set(-0.014, 0, 0.050);
        fingerLeft.castShadow = true;
        this.j5.add(fingerLeft);

        const tipLeft = new THREE.Mesh(
            new THREE.BoxGeometry(0.005, 0.012, 0.010),
            this.matNiryoRubber
        );
        tipLeft.position.set(-0.011, 0, 0.062);
        this.j5.add(tipLeft);

        // Right Finger
        const fingerRight = new THREE.Mesh(
            new THREE.BoxGeometry(0.007, 0.014, 0.030),
            this.matNiryoGripper
        );
        fingerRight.position.set(0.014, 0, 0.050);
        fingerRight.castShadow = true;
        this.j5.add(fingerRight);

        const tipRight = new THREE.Mesh(
            new THREE.BoxGeometry(0.005, 0.012, 0.010),
            this.matNiryoRubber
        );
        tipRight.position.set(0.011, 0, 0.062);
        this.j5.add(tipRight);

        // 6 articulated joint groups
        this.joints = [this.j0, this.j1, this.j2, this.j3, this.j4, this.j5];
    }

}
