import streamlit as st
import keyring
import asyncio
import nest_asyncio # For running asyncio in Streamlit
import re
import time
from datetime import datetime, timedelta
from json.decoder import JSONDecodeError
from random import gammavariate
from typing import Awaitable, Callable, List, Optional, Tuple, Union
import telegram

# Apply nest_asyncio to allow nested event loops
nest_asyncio.apply()

from ktx import (
    Korail,
    KorailError,
    ReserveOption,
    TrainType,
    AdultPassenger,
    ChildPassenger,
    SeniorPassenger,
    Disability1To3Passenger,
    Disability4To6Passenger,
)

from srt import (
    SRT,
    SRTError,
    SRTNetFunnelError,
    SeatType,
    Adult,
    Child,
    Senior,
    Disability1To3,
    Disability4To6,
)


STATIONS = {
    "SRT": [
        "수서",
        "동탄",
        "평택지제",
        "경주",
        "곡성",
        "공주",
        "광주송정",
        "구례구",
        "김천(구미)",
        "나주",
        "남원",
        "대전",
        "동대구",
        "마산",
        "목포",
        "밀양",
        "부산",
        "서대구",
        "순천",
        "여수EXPO",
        "여천",
        "오송",
        "울산(통도사)",
        "익산",
        "전주",
        "정읍",
        "진영",
        "진주",
        "창원",
        "창원중앙",
        "천안아산",
        "포항",
    ],
    "KTX": [
        "서울",
        "용산",
        "영등포",
        "광명",
        "수원",
        "천안아산",
        "오송",
        "대전",
        "서대전",
        "김천구미",
        "동대구",
        "경주",
        "포항",
        "밀양",
        "구포",
        "부산",
        "울산(통도사)",
        "마산",
        "창원중앙",
        "경산",
        "논산",
        "익산",
        "정읍",
        "광주송정",
        "목포",
        "전주",
        "순천",
        "여수EXPO",
        "청량리",
        "강릉",
        "행신",
        "정동진",
    ],
}
DEFAULT_STATIONS = {
    "SRT": ["수서", "대전", "동대구", "부산"],
    "KTX": ["서울", "대전", "동대구", "부산"],
}

# 예약 간격 (평균 간격 (초) = SHAPE * SCALE): gamma distribution (1.25 +/- 0.25 s)
RESERVE_INTERVAL_SHAPE = 4
RESERVE_INTERVAL_SCALE = 0.25
RESERVE_INTERVAL_MIN = 0.25

WAITING_BAR = ["|", "/", "-", "\\"] # Streamlit에서는 직접적인 애니메이션 구현이 어려울 수 있음

RailType = Union[str, None]
ChoiceType = Union[int, None]


def main():
    st.title("SRTGO - 열차 예매 도우미")

    MENU_CHOICES = {
        "예매 시작": 1,
        "예매 확인/결제/취소": 2,
        # "로그인 설정": 3,
        # "텔레그램 설정": 4,
        # "카드 설정": 5,
        "역 설정": 6,
        # "역 직접 수정": 7,
        # "예매 옵션 설정": 8,
        # "나가기": -1,
    }

    choice_name = st.sidebar.selectbox(
        "메뉴 선택", list(MENU_CHOICES.keys())
    )
    choice = MENU_CHOICES[choice_name]

    if choice == -1:
        st.stop()

    rail_type = None
    if choice in {1, 2, 3, 6, 7}:
        rail_type_name = st.sidebar.radio(
            "열차 선택", ["SRT", "KTX"]
        )
        rail_type = rail_type_name

    if choice == 1:
        reserve(rail_type, debug=False) # debug는 일단 False로 고정
    elif choice == 2:
        check_reservation(rail_type, debug=False)
    elif choice == 3:
        set_login(rail_type, debug=False)
    elif choice == 4:
        set_telegram()
    elif choice == 5:
        set_card()
    elif choice == 6:
        set_station(rail_type)
    elif choice == 7:
        edit_station(rail_type)
    elif choice == 8:
        set_options()


def set_station(rail_type: RailType) -> bool:
    st.subheader(f"{rail_type} 역 설정")
    stations, default_station_key = get_station(rail_type)

    selected = st.multiselect(
        "역 선택",
        options=stations,
        default=default_station_key,
        help="여러 역을 선택할 수 있습니다."
    )

    if not selected:
        st.warning("선택된 역이 없습니다.")
        return False

    selected_stations = ",".join(selected)
    keyring.set_password(rail_type, "station", selected_stations)
    st.success(f"선택된 역: {selected_stations}")
    return True


def edit_station(rail_type: RailType) -> bool:
    st.subheader(f"{rail_type} 역 직접 수정")
    current_stations = keyring.get_password(rail_type, "station") or ""
    
    edited_stations_str = st.text_input(
        "역 수정 (예: 수서,대전,동대구)",
        value=current_stations,
        help="쉼표(,)로 구분하여 역 이름을 입력하세요."
    )

    if not edited_stations_str:
        st.warning("선택된 역이 없습니다.")
        return False

    selected = [s.strip() for s in edited_stations_str.split(",")]

    # Verify all stations contain Korean characters
    hangul = re.compile("[가-힣]+")
    for station in selected:
        if not hangul.search(station):
            st.error(f"'{station}'는 잘못된 입력입니다. 기본 역으로 설정합니다.")
            selected = DEFAULT_STATIONS[rail_type]
            break

    selected_stations = ",".join(selected)
    keyring.set_password(rail_type, "station", selected_stations)
    st.success(f"선택된 역: {selected_stations}")
    return True


def get_station(rail_type: RailType) -> Tuple[List[str], List[str]]: # Changed return type for default_station_key
    stations = STATIONS[rail_type]
    station_key = keyring.get_password(rail_type, "station")

    if not station_key:
        return stations, DEFAULT_STATIONS[rail_type]

    valid_keys = [x for x in station_key.split(",")]
    return stations, valid_keys


def set_options():
    st.subheader("예매 옵션 설정")
    default_options = get_options()
    
    option_choices = {
        "어린이": "child",
        "경로우대": "senior",
        "중증장애인": "disability1to3",
        "경증장애인": "disability4to6",
        "KTX만": "ktx",
    }
    
    selected_options_labels = st.multiselect(
        "예매 옵션 선택",
        options=list(option_choices.keys()),
        default=[label for label, key in option_choices.items() if key in default_options],
        help="예매 시 적용할 옵션을 선택하세요."
    )
    
    options = [option_choices[label] for label in selected_options_labels]
    keyring.set_password("SRT", "options", ",".join(options))
    st.success(f"선택된 옵션: {', '.join(selected_options_labels) if selected_options_labels else '없음'}")


def get_options():
    options = keyring.get_password("SRT", "options") or ""
    return options.split(",") if options else []


def set_telegram() -> bool:
    st.subheader("텔레그램 설정")
    token = keyring.get_password("telegram", "token") or ""
    chat_id = keyring.get_password("telegram", "chat_id") or ""

    new_token = st.text_input("텔레그램 토큰", value=token, type="password")
    new_chat_id = st.text_input("텔레그램 Chat ID", value=chat_id)

    if st.button("텔레그램 설정 저장"):
        try:
            keyring.set_password("telegram", "ok", "1")
            keyring.set_password("telegram", "token", new_token)
            keyring.set_password("telegram", "chat_id", new_chat_id)
            
            tgprintf = get_telegram()
            if tgprintf:
                asyncio.run(tgprintf("[SRTGO] 텔레그램 설정 완료"))
                st.success("텔레그램 설정 완료 및 테스트 메시지 전송 성공!")
            else:
                st.warning("텔레그램 토큰 또는 Chat ID가 유효하지 않아 테스트 메시지를 보낼 수 없습니다.")
            return True
        except Exception as err:
            st.error(f"텔레그램 설정 중 오류 발생: {err}")
            keyring.delete_password("telegram", "ok")
            return False
    return False


def get_telegram() -> Optional[Callable[[str], Awaitable[None]]]:
    token = keyring.get_password("telegram", "token")
    chat_id = keyring.get_password("telegram", "chat_id")

    if not token or not chat_id:
        return None

    async def tgprintf(text):
        try:
            bot = telegram.Bot(token=token)
            async with bot:
                await bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            st.error(f"텔레그램 메시지 전송 실패: {e}")

    return tgprintf


def set_card() -> None:
    st.subheader("카드 설정")
    card_info = {
        "number": keyring.get_password("card", "number") or "",
        "password": keyring.get_password("card", "password") or "",
        "birthday": keyring.get_password("card", "birthday") or "",
        "expire": keyring.get_password("card", "expire") or "",
    }

    new_number = st.text_input("신용카드 번호 (하이픈 제외)", value=card_info["number"], type="password")
    new_password = st.text_input("카드 비밀번호 앞 2자리", value=card_info["password"], type="password")
    new_birthday = st.text_input("생년월일 (YYMMDD) / 사업자등록번호", value=card_info["birthday"], type="password")
    new_expire = st.text_input("카드 유효기간 (YYMM)", value=card_info["expire"], type="password")

    if st.button("카드 정보 저장"):
        if new_number and new_password and new_birthday and new_expire:
            keyring.set_password("card", "number", new_number)
            keyring.set_password("card", "password", new_password)
            keyring.set_password("card", "birthday", new_birthday)
            keyring.set_password("card", "expire", new_expire)
            keyring.set_password("card", "ok", "1")
            st.success("카드 정보가 성공적으로 저장되었습니다.")
        else:
            st.error("모든 카드 정보를 입력해주세요.")


def pay_card(rail, reservation) -> bool:
    if keyring.get_password("card", "ok"):
        birthday = keyring.get_password("card", "birthday")
        try:
            return rail.pay_with_card(
                reservation,
                keyring.get_password("card", "number"),
                keyring.get_password("card", "password"),
                birthday,
                keyring.get_password("card", "expire"),
                0,
                "J" if len(birthday) == 6 else "S",
            )
        except Exception as e:
            st.error(f"카드 결제 중 오류 발생: {e}")
            return False
    st.warning("카드 정보가 설정되지 않았습니다.")
    return False


def set_login(rail_type="SRT", debug=False):
    st.subheader(f"{rail_type} 로그인 설정")
    credentials = {
        "id": keyring.get_password(rail_type, "id") or "",
        "pass": keyring.get_password(rail_type, "pass") or "",
    }

    new_id = st.text_input(f"{rail_type} 계정 아이디 (멤버십 번호, 이메일, 전화번호)", value=credentials["id"])
    new_pass = st.text_input(f"{rail_type} 계정 패스워드", value=credentials["pass"], type="password")

    if st.button(f"{rail_type} 로그인 정보 저장 및 테스트"):
        if not new_id or not new_pass:
            st.error("아이디와 비밀번호를 모두 입력해주세요.")
            return False
        try:
            if rail_type == "SRT":
                SRT(new_id, new_pass, auto_login=True, verbose=debug)
            else:
                Korail(new_id, new_pass, auto_login=True, verbose=debug)

            keyring.set_password(rail_type, "id", new_id)
            keyring.set_password(rail_type, "pass", new_pass)
            keyring.set_password(rail_type, "ok", "1")
            st.success(f"{rail_type} 로그인 성공!")
            return True
        except (SRTError, KorailError) as err:
            st.error(f"로그인 실패: {err}")
            keyring.delete_password(rail_type, "ok")
            return False
        except Exception as err:
            st.error(f"예상치 못한 오류 발생: {err}")
            keyring.delete_password(rail_type, "ok")
            return False
    return False


@st.cache_resource # Cache the rail object to avoid re-logging in on every rerun
def get_rail_instance(rail_type, debug):
    user_id = keyring.get_password(rail_type, "id")
    password = keyring.get_password(rail_type, "pass")

    if not user_id or not password:
        st.error(f"{rail_type} 로그인 정보가 설정되지 않았습니다. '로그인 설정' 메뉴에서 설정해주세요.")
        return None

    try:
        if rail_type == "SRT":
            return SRT(user_id, password, auto_login=True, verbose=debug)
        else:
            return Korail(user_id, password, auto_login=True, verbose=debug)
    except (SRTError, KorailError) as err:
        st.error(f"로그인 실패: {err}. '로그인 설정' 메뉴에서 다시 시도해주세요.")
        return None
    except Exception as err:
        st.error(f"열차 객체 생성 중 예상치 못한 오류 발생: {err}")
        return None


def reserve(rail_type="SRT", debug=False):
    st.subheader(f"{rail_type} 예매 시작")
    rail = get_rail_instance(rail_type, debug=debug)
    if rail is None:
        return

    is_srt = rail_type == "SRT"

    now = datetime.now() + timedelta(minutes=10)
    today = now.strftime("%Y%m%d")
    this_time = now.strftime("%H%M%S")

    defaults = {
        "departure": keyring.get_password(rail_type, "departure")
        or ("수서" if is_srt else "서울"),
        "arrival": keyring.get_password(rail_type, "arrival") or "동대구",
        "date": keyring.get_password(rail_type, "date") or today,
        "time": keyring.get_password(rail_type, "time") or "120000",
        "adult": int(keyring.get_password(rail_type, "adult") or 1),
        "child": int(keyring.get_password(rail_type, "child") or 0),
        "senior": int(keyring.get_password(rail_type, "senior") or 0),
        "disability1to3": int(keyring.get_password(rail_type, "disability1to3") or 0),
        "disability4to6": int(keyring.get_password(rail_type, "disability4to6") or 0),
    }

    if defaults["departure"] == defaults["arrival"]:
        defaults["arrival"] = (
            "동대구" if defaults["departure"] in ("수서", "서울") else None
        )
        defaults["departure"] = (
            defaults["departure"]
            if defaults["arrival"]
            else ("수서" if is_srt else "서울")
        )

    stations, station_key = get_station(rail_type)
    options = get_options()

    if is_srt:
        max_days = 30 if now.hour >= 7 else 29
    else:
        max_days = 31 if now.hour >= 7 else 30

    date_choices = [
        ((now + timedelta(days=i)).strftime("%Y/%m/%d %a"), (now + timedelta(days=i)).strftime("%Y%m%d"))
        for i in range(max_days + 1)
    ]
    time_choices = [(f"{h:02d}", f"{h:02d}0000") for h in range(24)]

    col1, col2 = st.columns(2)
    with col1:
        departure = st.selectbox(
            "출발역",
            options=station_key,
            index=station_key.index(defaults["departure"]) if defaults["departure"] in station_key else 0
        )
    with col2:
        arrival = st.selectbox(
            "도착역",
            options=station_key,
            index=station_key.index(defaults["arrival"]) if defaults["arrival"] in station_key else 0
        )

    selected_date_label, selected_date_value = st.selectbox(
        "출발 날짜",
        options=date_choices,
        format_func=lambda x: x[0],
        index=[i for i, (label, val) in enumerate(date_choices) if val == defaults["date"]][0] if defaults["date"] in [val for label, val in date_choices] else 0
    )
    date = selected_date_value

    selected_time_label, selected_time_value = st.selectbox(
        "출발 시각",
        options=time_choices,
        format_func=lambda x: x[0],
        index=[i for i, (label, val) in enumerate(time_choices) if val == defaults["time"]][0] if defaults["time"] in [val for label, val in time_choices] else 0
    )
    time_str = selected_time_value

    st.write("---")
    st.subheader("승객 정보")

    passenger_types_map = {
        "adult": {"label": "성인", "class_srt": Adult, "class_ktx": AdultPassenger},
        "child": {"label": "어린이", "class_srt": Child, "class_ktx": ChildPassenger},
        "senior": {"label": "경로우대", "class_srt": Senior, "class_ktx": SeniorPassenger},
        "disability1to3": {"label": "중증장애인", "class_srt": Disability1To3, "class_ktx": Disability1To3Passenger},
        "disability4to6": {"label": "경증장애인", "class_srt": Disability4To6, "class_ktx": Disability4To6Passenger},
    }

    passenger_counts = {}
    total_count = 0
    
    # Always show adult passenger count
    adult_count = st.number_input("성인 승객수", min_value=0, max_value=9, value=defaults["adult"], key="adult_count")
    passenger_counts["adult"] = adult_count
    total_count += adult_count

    for key, p_info in passenger_types_map.items():
        if key != "adult" and key in options: # Only show if enabled in options
            count = st.number_input(f"{p_info['label']} 승객수", min_value=0, max_value=9, value=defaults[key], key=f"{key}_count")
            passenger_counts[key] = count
            total_count += count
        elif key != "adult":
            passenger_counts[key] = 0 # Ensure non-selected types are 0

    if total_count == 0:
        st.error("승객수는 0이 될 수 없습니다.")
        return
    if total_count >= 10:
        st.error("승객수는 10명을 초과할 수 없습니다.")
        return

    passengers = []
    for key, count in passenger_counts.items():
        if count > 0:
            if is_srt:
                passengers.append(passenger_types_map[key]["class_srt"](count))
            else:
                passengers.append(passenger_types_map[key]["class_ktx"](count))

    msg_passengers = [
        f"{passenger_types_map[key]['label']} {count}명"
        for key, count in passenger_counts.items() if count > 0
    ]
    st.info(f"선택된 승객: {', '.join(msg_passengers)}")

    st.write("---")
    st.subheader("예매 옵션")

    seat_type_options = {
        "일반실 우선": SeatType.GENERAL_FIRST if is_srt else ReserveOption.GENERAL_FIRST,
        "일반실만": SeatType.GENERAL_ONLY if is_srt else ReserveOption.GENERAL_ONLY,
        "특실 우선": SeatType.SPECIAL_FIRST if is_srt else ReserveOption.SPECIAL_FIRST,
        "특실만": SeatType.SPECIAL_ONLY if is_srt else ReserveOption.SPECIAL_ONLY,
    }
    selected_seat_type_label = st.radio("좌석 유형 선택", list(seat_type_options.keys()))
    selected_seat_type = seat_type_options[selected_seat_type_label]

    pay_with_card_option = st.checkbox("예매 시 카드 결제", value=False)

    if st.button("열차 검색 및 예매 시작"):
        if departure == arrival:
            st.error("출발역과 도착역이 같습니다.")
            return

        # Save preferences
        keyring.set_password(rail_type, "departure", departure)
        keyring.set_password(rail_type, "arrival", arrival)
        keyring.set_password(rail_type, "date", date)
        keyring.set_password(rail_type, "time", time_str)
        for key, count in passenger_counts.items():
            keyring.set_password(rail_type, key, str(count))

        # Adjust time if needed
        if date == today and int(time_str) < int(this_time):
            time_str = this_time

        params = {
            "dep": departure,
            "arr": arrival,
            "date": date,
            "time": time_str,
            "passengers": [passenger_types_map["adult"]["class_srt"](total_count)] if is_srt else [passenger_types_map["adult"]["class_ktx"](total_count)],
            **({"available_only": False} if is_srt else {"include_no_seats": True, **({"train_type": TrainType.KTX} if "ktx" in options else {})}),
        }

        st.info("열차를 검색 중입니다...")
        train_search_placeholder = st.empty()
        
        i_try = 0
        start_time = time.time()
        
        while True:
            i_try += 1
            elapsed_time = time.time() - start_time
            hours, remainder = divmod(int(elapsed_time), 3600)
            minutes, seconds = divmod(remainder, 60)
            
            train_search_placeholder.text(
                f"예매 대기 중... {WAITING_BAR[i_try % len(WAITING_BAR)]} {i_try:4d} ({hours:02d}:{minutes:02d}:{seconds:02d})"
            )

            try:
                trains = rail.search_train(**params)
                
                available_trains = []
                for train in trains:
                    if _is_seat_available(train, selected_seat_type, rail_type):
                        available_trains.append(train)
                
                if available_trains:
                    st.success("예약 가능한 열차를 찾았습니다! 예매를 시도합니다.")
                    for train_to_reserve in available_trains:
                        try:
                            reserve_result = rail.reserve(train_to_reserve, passengers=passengers, option=selected_seat_type)
                            msg = f"{reserve_result}"
                            if hasattr(reserve_result, "tickets") and reserve_result.tickets:
                                msg += "\n" + "\n".join(map(str, reserve_result.tickets))

                            st.markdown(f"### 🎫 🎉 예매 성공!!! 🎉 🎫{msg}")

                            if pay_with_card_option and not reserve_result.is_waiting:
                                if pay_card(rail, reserve_result):
                                    st.markdown("### 💳 ✨ 결제 성공!!! ✨ 💳")
                                    msg += "\n결제 완료"
                                else:
                                    st.error("카드 결제에 실패했습니다.")
                            
                            tgprintf = get_telegram()
                            if tgprintf:
                                asyncio.run(tgprintf(msg))
                            return # Exit after successful reservation
                        except (SRTError, KorailError) as ex:
                            st.error(f"예매 시도 중 오류 발생: {ex}")
                            # Continue trying other available trains or re-search
                        except Exception as ex:
                            st.error(f"예매 시도 중 예상치 못한 오류 발생: {ex}")
                            # Continue trying other available trains or re-search
                    st.warning("모든 예약 가능한 열차에 대한 예매 시도가 실패했습니다. 다시 검색합니다.")
                else:
                    st.info("현재 예약 가능한 열차가 없습니다. 다시 검색합니다.")
                
                _sleep() # Wait before next search attempt

            except (SRTError, KorailError) as ex:
                msg = str(ex)
                if "정상적인 경로로 접근 부탁드립니다" in msg or isinstance(ex, SRTNetFunnelError):
                    st.error(f"NetFunnel 오류 또는 비정상적인 접근: {ex}. 다시 시도합니다.")
                    rail.clear()
                elif "로그인 후 사용하십시오" in msg or "Need to Login" in msg:
                    st.error(f"로그인 세션이 만료되었습니다: {ex}. 다시 로그인합니다.")
                    rail = get_rail_instance(rail_type, debug=debug) # Re-login
                    if rail is None:
                        st.error("로그인 재시도 실패. 프로그램을 종료합니다.")
                        return
                elif not any(err in msg for err in ("잔여석없음", "사용자가 많아 접속이 원활하지 않습니다", "예약대기 접수가 마감되었습니다", "예약대기자한도수초과", "Sold out")):
                    if _handle_error(ex, st.error): # Pass st.error for displaying error
                        st.warning("오류 발생. 계속 시도합니다.")
                    else:
                        st.error("사용자가 예매를 중단했습니다.")
                        return
                _sleep()

            except JSONDecodeError as ex:
                st.error(f"JSON 디코딩 오류 발생: {ex}. 다시 로그인합니다.")
                rail = get_rail_instance(rail_type, debug=debug) # Re-login
                if rail is None:
                    st.error("로그인 재시도 실패. 프로그램을 종료합니다.")
                    return
                _sleep()

            except ConnectionError as ex:
                st.error(f"연결 오류 발생: {ex}. 다시 로그인합니다.")
                rail = get_rail_instance(rail_type, debug=debug) # Re-login
                if rail is None:
                    st.error("로그인 재시도 실패. 프로그램을 종료합니다.")
                    return
                _sleep()

            except Exception as ex:
                st.error(f"예상치 못한 오류 발생: {ex}")
                if _handle_error(ex, st.error):
                    st.warning("오류 발생. 계속 시도합니다.")
                else:
                    st.error("사용자가 예매를 중단했습니다.")
                    return
                rail = get_rail_instance(rail_type, debug=debug) # Re-login


def _sleep():
    time.sleep(
        gammavariate(RESERVE_INTERVAL_SHAPE, RESERVE_INTERVAL_SCALE)
        + RESERVE_INTERVAL_MIN
    )


def _handle_error(ex, error_display_func):
    msg = f"오류 발생: {ex}"
    error_display_func(msg)
    tgprintf = get_telegram()
    if tgprintf:
        asyncio.run(tgprintf(msg))
    
    # In Streamlit, we can't directly ask for confirmation in a loop like CLI.
    # For now, we'll just log and continue. A more robust solution would involve
    # a button to continue or stop, or a timeout.
    st.warning("오류가 발생했지만, 예매 시도를 계속합니다. 중단하려면 새로고침하세요.")
    return True # Always return True to continue for now


def _is_seat_available(train, seat_type, rail_type):
    if rail_type == "SRT":
        if not train.seat_available():
            return train.reserve_standby_available()
        if seat_type in [SeatType.GENERAL_FIRST, SeatType.SPECIAL_FIRST]:
            return train.seat_available()
        if seat_type == SeatType.GENERAL_ONLY:
            return train.general_seat_available()
        return train.special_seat_available()
    else: # KTX
        if not train.has_seat():
            return train.has_waiting_list()
        if seat_type in [ReserveOption.GENERAL_FIRST, ReserveOption.SPECIAL_FIRST]:
            return train.has_seat()
        if seat_type == ReserveOption.GENERAL_ONLY:
            return train.has_general_seat()
        return train.has_general_seat()


def check_reservation(rail_type="SRT", debug=False):
    st.subheader(f"{rail_type} 예매 확인/결제/취소")
    rail = get_rail_instance(rail_type, debug=debug)
    if rail is None:
        return

    try:
        reservations = rail.get_reservations() if rail_type == "SRT" else rail.reservations()
        tickets = [] if rail_type == "SRT" else rail.tickets()

        all_reservations = []
        for t in tickets:
            t.is_ticket = True
            all_reservations.append(t)
        for r in reservations:
            if hasattr(r, "paid") and r.paid:
                r.is_ticket = True
            else:
                r.is_ticket = False
            all_reservations.append(r)

        if not all_reservations:
            st.info("예약 내역이 없습니다.")
            return

        st.write("---")
        st.subheader("예약 내역")
        
        reservation_options = [str(res) for res in all_reservations]
        selected_reservation_str = st.selectbox("예약 선택", options=reservation_options)

        if selected_reservation_str:
            selected_reservation_index = reservation_options.index(selected_reservation_str)
            selected_reservation = all_reservations[selected_reservation_index]

            st.write(f"선택된 예약: {selected_reservation}")
            if hasattr(selected_reservation, "tickets") and selected_reservation.tickets:
                st.write("---")
                st.subheader("티켓 상세")
                for ticket in selected_reservation.tickets:
                    st.write(f"- {ticket}")

            if not selected_reservation.is_ticket and not selected_reservation.is_waiting:
                st.write("---")
                st.subheader("결제 대기 승차권")
                action = st.radio("결제 또는 취소", ["결제하기", "취소하기"])
                if st.button("실행"):
                    if action == "결제하기":
                        if pay_card(rail, selected_reservation):
                            st.success("💳 ✨ 결제 성공!!! ✨ 💳")
                        else:
                            st.error("카드 결제에 실패했습니다.")
                    elif action == "취소하기":
                        if st.checkbox("정말 취소하시겠습니까?", key="confirm_cancel_unpaid"):
                            rail.cancel(selected_reservation)
                            st.success("예약이 취소되었습니다.")
                        else:
                            st.info("취소 요청이 철회되었습니다.")
            elif st.button("예약/티켓 취소"):
                if st.checkbox("정말 취소하시겠습니까?", key="confirm_cancel_paid"):
                    try:
                        if selected_reservation.is_ticket:
                            rail.refund(selected_reservation)
                        else:
                            rail.cancel(selected_reservation)
                        st.success("예약/티켓이 취소되었습니다.")
                    except Exception as err:
                        st.error(f"취소 중 오류 발생: {err}")
                else:
                    st.info("취소 요청이 철회되었습니다.")
            
            if st.button("텔레그램으로 예매 정보 전송"):
                out = []
                out.append("[ 예매 내역 ]")
                out.append(f"🚅{selected_reservation}")
                if rail_type == "SRT" and hasattr(selected_reservation, "tickets") and selected_reservation.tickets:
                    out.extend(map(str, selected_reservation.tickets))
                
                tgprintf = get_telegram()
                if tgprintf:
                    asyncio.run(tgprintf("\n".join(out)))
                    st.success("텔레그램으로 예매 정보가 전송되었습니다.")
                else:
                    st.warning("텔레그램 설정이 완료되지 않아 메시지를 보낼 수 없습니다.")

    except (SRTError, KorailError) as ex:
        st.error(f"예약 내역 조회 중 오류 발생: {ex}")
    except Exception as ex:
        st.error(f"예상치 못한 오류 발생: {ex}")


if __name__ == "__main__":
    main()